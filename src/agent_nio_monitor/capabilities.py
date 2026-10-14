"""Feature-detect: descobre o que a API do vendor realmente expoe.

Por que: eu assumi que a instancia era n8n 1.102.4 e declarei, com base no
OpenAPI daquela tag, que `GET /workflows/{id}/history` e `authors` nao existiam.
A instancia real respondeu com `activeVersion` e `authors` preenchido. A
assuncao estava errada e o erro saiu do plano.

Regra do projeto: NUNCA assumir versao. Sonda, grava, adapta. Se o vendor mudar
ou se o Gateway na frente mudar, o boot loga a diferenca em vez de quebrar em
producao as 3 da manha.

Este modulo responde tambem a pergunta que ficou aberta na conversa: "qual a
versao real do n8n?" -- o boot imprime.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

# Endpoints que valem a pena sondar. `probe` = metodo + caminho. `optional` = a
# ausencia dele nao quebra o agente, so reduz funcionalidade.
ENDPOINTS: tuple[tuple[str, str, str, bool], ...] = (
    # n8n
    ("n8n", "GET", "/api/v1/workflows", "core", True),
    ("n8n", "GET", "/api/v1/executions", "core", True),
    ("n8n", "GET", "/api/v1/credentials", "core", True),
    ("n8n", "GET", "/api/v1/projects", "tenant", True),
    ("n8n", "GET", "/api/v1/users", "users", False),
    ("n8n", "GET", "/api/v1/workflows/{id}/history", "versioning", False),
    ("n8n", "GET", "/api/v1/workflows/{id}/versions/{vid}", "versioning", False),
    ("n8n", "GET", "/api/v1/workflows/{id}/diff", "versioning", False),
    ("n8n", "GET", "/healthz", "health", True),
    ("n8n", "GET", "/healthz/readiness", "health", False),
    ("n8n", "GET", "/metrics", "metrics", False),
    # Portainer (prefixo relativo ao host da API)
    ("portainer", "GET", "/api/endpoints", "core", True),
    ("portainer", "GET", "/api/stacks", "core", True),
    ("portainer", "GET", "/api/users", "users", False),
)

# Fan-out da sonda. Suficiente para 11 rotas em rede interna sem abrir 11
# conexoes de uma vez, e suficiente para nao serializar o boot.
CONCORRENCIA = 6

# Classe para reportar resultado de sonda de capacidades
@dataclass(frozen=True)
class Capability:
    source: str
    method: str
    path: str
    feature: str
    required: bool
    available: bool
    status_code: int | None = None
    detail: str = ""

    @property
    def key(self) -> str:
        return f"{self.source}:{self.feature}"


@dataclass
class CapabilityReport:
    source: str = "n8n"
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    n8n_version: str | None = None
    items: list[Capability] = field(default_factory=list)

    def has(self, source: str, feature: str) -> bool:
        return any(c.source == source and c.feature == feature and c.available for c in self.items)

    def missing_core(self) -> list[Capability]:
        return [c for c in self.items if c.required and not c.available]

    def as_rows(self) -> list[tuple[str, str, str, bool, str]]:
        return [
            (c.source, c.feature, c.path, c.available, c.detail or str(c.status_code or ""))
            for c in self.items
        ]

# Funções auxiliares para sondar endpoints e resumir resultados 
def _summarise(status: int, path: str = "") -> str:
    """401 e 403 NAO sao o mesmo problema, e dizer que sao custou uma ida ao
    cadastro de usuario na porta errada.

    Verificado na rede do dono: o mesmo token do Portainer devolve `401 Invalid
    JWT token` numa instancia e `200` (com a lista de endpoints) noutra. Com a
    mensagem antiga ("sem_permissao, verifique owner/admin") o problema real era o
    endereco, e a orientacao levava para o lugar errado.

    - 401: a chave nao foi aceita. Token invalido, expirado ou de outra
      instancia. Refazer a credencial NAO adianta; o endereco esta errado.
    - 403: a chave foi aceita e o usuario nao tem o papel exigido. Aqui sim e
      permissao.
    """
    if status == 404 and path == "/metrics":
        # No n8n, /metrics existe mas vem 404 quando N8N_METRICS=true nao esta
        # ligado. Nao e "versao sem o recurso": e configuracao.
        return "desligado (exige N8N_METRICS=true no servidor)"
    return {
        200: "ok",
        401: "chave NAO aceita (token invalido, expirado ou de outra instancia)",
        403: "chave aceita, mas o usuario nao tem o papel exigido (admin)",
        404: "nao_existe_nesta_versao",
        405: "metodo_nao_suportado",
        400: "existe_mas_param_invalido (id de teste?)",
        429: "rate_limited",
        502: "gateway",
        503: "indisponivel",
    }.get(status, f"http_{status}")


async def _probe_one(
    client: httpx.AsyncClient, base_url: str, source: str, method: str, path: str
) -> tuple[int, str]:
    url = f"{base_url.rstrip('/')}{path}"
    try:
        resp = await client.request(method, url, timeout=10.0)
    except httpx.HTTPError as exc:
        return 0, f"erro_transporte: {type(exc).__name__}"
    if resp.status_code == 404 and "{" in path:
        # endpoint parametrizado: 404 pode ser "id inexistente", nao "rota ausente".
        # Probe de verdade so no caso concreto vem depois; aqui registramos como ambiguo.
        return 404, "ambiguo_404 (id de teste inexistente ou rota ausente)"
    return resp.status_code, ""


async def probe_n8n(base_url: str, api_key: str) -> CapabilityReport:
    headers = {"X-N8N-API-KEY": api_key, "Accept": "application/json"}
    report = CapabilityReport(source="n8n")
    async with httpx.AsyncClient(headers=headers) as client:
        # Versao e OPCIONAL. Verificado nesta instalacao: /healthz devolve so
        # {"status":"ok"} e a UI nao tem versao no HTML.
        #
        # `str(None)` produz a string "None", que e TRUTHY -- por isso o
        # `is not None` vem ANTES da conversao. Sem isso a saida mostra
        # "VERSAO REPORTADA PELO N8N: None" como se fosse um valor.
        try:
            resp = await client.get(f"{base_url.rstrip('/')}/healthz", timeout=10.0)
            if resp.status_code == 200:
                try:
                    versao = resp.json().get("version")
                    report.n8n_version = str(versao) if versao is not None else None
                except ValueError:
                    report.n8n_version = None
        except httpx.HTTPError as exc:
            log.warning("n8n_healthz_falhou", erro=type(exc).__name__)

        # Paralelo, e nao em laco: 11 rotas x 10 s de timeout sao ate 110 s se
        # sequencial -- e a sonda roda no boot, entao esse tempo atrasa o
        # primeiro ciclo de coleta. `CONCORRENCIA` limita o fan-out, que numa
        # rede interna nao precisa chegar a 11 de uma vez.
        rotas_n8n = [e for e in ENDPOINTS if e[0] == "n8n"]
        # Semaforo LOCAL, nao de modulo: `asyncio.Semaphore` se prende ao event
        # loop que o usou primeiro, e cada teste roda no seu proprio loop. Um
        # semaforo de modulo reused entre loops e a forma de um "attached to a
        # different loop" que so aparece em um dos testes.
        sem = asyncio.Semaphore(CONCORRENCIA)

        async def _uma(rota) -> None:  # noqa: ANN001
            source, method, path, feature, required = rota
            async with sem:
                status, detail = await _probe_one(client, base_url, source, method, path)
            # 400 em rota parametrizada = rota existe, id de teste nao.
            report.items.append(
                Capability(
                    source=source,
                    method=method,
                    path=path,
                    feature=feature,
                    required=required,
                    available=status in (200, 400, 405),
                    status_code=status,
                    detail=detail or _summarise(status, path),
                )
            )

        await asyncio.gather(*(_uma(rota) for rota in rotas_n8n))
    return report


async def probe_portainer(base_url: str, api_key: str) -> CapabilityReport:
    # Header do Portainer e X-API-Key, NAO Authorization: Bearer (doc original errava).
    headers = {"X-API-Key": api_key, "Accept": "application/json"}
    report = CapabilityReport(source="portainer")
    async with httpx.AsyncClient(headers=headers, verify=False) as client:
        sem = asyncio.Semaphore(CONCORRENCIA)

        async def _uma(rota) -> None:  # noqa: ANN001
            source, method, path, feature, required = rota
            async with sem:
                status, detail = await _probe_one(client, base_url, source, method, path)
            report.items.append(
                Capability(
                    source=source,
                    method=method,
                    path=path,
                    feature=feature,
                    required=required,
                    available=status in (200, 403, 405),
                    status_code=status,
                    detail=detail or _summarise(status, path),
                )
            )

        await asyncio.gather(*(_uma(rota) for rota in ENDPOINTS if rota[0] == "portainer"))
    return report


def fontes_do_config(cfg) -> dict[str, dict[str, str]]:  # noqa: ANN001 - AppConfig
    """Credenciais discoveries para sondar, a partir da config ativa.

    Fica aqui, e nao em `main.py`, porque `doctor` e o boot do agente precisam
    da MESMA montagem. Antes cada um fazia a sua, e o boot nao sondava nada --
    `save_capabilities` nunca era chamado fora de teste.
    """
    import os

    env = cfg.active
    fontes: dict[str, dict[str, str]] = {}
    if os.environ.get(env.n8n.api_key_env):
        fontes["n8n"] = {"url": env.n8n.url, "api_key": os.environ[env.n8n.api_key_env]}
    if env.portainer and os.environ.get(env.portainer.api_key_env):
        fontes["portainer"] = {
            # `PORTAINER_URL` sobrescreve SO no doctor: permite sondar um
            # endereco alternativo sem editar a config versionada.
            "url": os.environ.get("PORTAINER_URL", env.portainer.url),
            "api_key": os.environ[env.portainer.api_key_env],
        }
    return fontes


async def detect(
    sources: dict[str, dict[str, str]], *, probe_parallel: bool = True
) -> dict[str, CapabilityReport]:
    """Sonda todas as fontes. Nao levanta: quem chama decide o que fazer."""
    jobs: dict[str, Any] = {}
    if "n8n" in sources:
        jobs["n8n"] = probe_n8n(sources["n8n"]["url"], sources["n8n"]["api_key"])
    if "portainer" in sources:
        jobs["portainer"] = probe_portainer(
            sources["portainer"]["url"], sources["portainer"]["api_key"]
        )
    if not jobs:
        return {}

    if probe_parallel:
        resolved = await asyncio.gather(*jobs.values())
    else:
        resolved = [await j for j in jobs.values()]
    reports = {name: rep for name, rep in zip(jobs.keys(), resolved, strict=True)}

    for name, rep in reports.items():
        log.info(
            "capabilities_detectadas",
            fonte=name,
            versao_n8n=rep.n8n_version,
            **{
                f"{c.feature}": c.available
                for c in rep.items
                if c.feature in ("versioning", "users", "health", "metrics", "tenant")
            },
        )
        for cap in rep.missing_core():
            log.error(
                "capability_core_ausente", fonte=cap.source, path=cap.path, detalhe=cap.detail
            )
    return reports
