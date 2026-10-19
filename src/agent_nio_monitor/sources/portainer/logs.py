"""Leitura incremental dos logs dos containers: erro novo, pico de erro conhecido e falha de trigger do n8n.

Medido em 19/10/2026: 35 containers, ~10 mil linhas no tail de 500, 13 com linhas de erro (um
com 239 da MESMA mensagem). Por isso a unidade e a ASSINATURA da mensagem, e nao a linha: o
catalogo (`resource`, kind `log-signature`) lembra o que ja foi visto e a media por leitura.

Primeira leitura de um container so SEMEIA o catalogo com o historico recente, sem alertar.
Rodizio: ate `max_containers_por_ciclo` por ciclo; o cursor (`since`) garante que nada se perde.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog

from ...config.schema import LogsConfig
from ...redaction import redact_text
from ...store.repository import CursorState, EventRecord, Repository, now_utc
from ..base import CollectorResult, _erro_total_de_endpoints
from .client import PortainerApiError, PortainerClient
from .donos import dono_da_stack, stack_do_container, stacks_por_nome
from .log_events import evento_falha_de_trigger, evento_log_de_erro
from .log_lines import assinatura, eh_erro, falha_de_trigger, separar

log = structlog.get_logger(__name__)

KIND = "log-signature"
SCOPE = "logs"
SEMENTE_LINHAS = 500
MAX_LINHAS = 2000
MAX_AMOSTRA = 300
MAX_LOG_NO_ALERTA = 1200
PESO_DA_MEDIA = 0.2
JANELA_DO_TRIGGER = timedelta(hours=1)


@dataclass
class Leitura:
    contagem: Counter = field(default_factory=Counter)
    amostras: dict[str, str] = field(default_factory=dict)
    linhas: dict[str, list[str]] = field(default_factory=dict)
    triggers: list[tuple[str, str, str, datetime]] = field(default_factory=list)
    ate: datetime | None = None


class ContainerLogCollector:
    source = "portainer"

    def __init__(self, client: PortainerClient, repo: Repository, cfg: LogsConfig) -> None:
        self.client, self.repo, self.cfg = client, repo, cfg
        self._ignorar = {nome: [re.compile(p) for p in padroes] for nome, padroes in cfg.ignorar.items()}
        self._catalogo: dict[str, dict] | None = None
        self._vez = 0
        self._triggers: dict[str, list[datetime]] = {}
        # Inicio do incidente de gatilho: abre na 1a falha com a janela vazia.
        self._inicio_trigger: dict[str, datetime] = {}

    async def check_logs(self, workflows_por_nome: dict[str, str] | None = None) -> CollectorResult:
        try:
            endpoints = [e for e in await self.client.list_endpoints() if e.get("Id") is not None]
        except PortainerApiError as exc:
            return CollectorResult(error=str(exc))
        alvos, escuros = await self._alvos(endpoints)
        stacks = await stacks_por_nome(self.client)
        cursores = self._ler_cursores()
        eventos: list[EventRecord] = []
        falhas = 0
        for endpoint_id, row in alvos:
            try:
                eventos += await self._container(endpoint_id, row, cursores, stacks, workflows_por_nome or {})
            except PortainerApiError as exc:
                log.warning("log_do_container_ilegivel", container=_nome(row), erro=str(exc))
                falhas += 1
        self._gravar_cursores(cursores)
        return CollectorResult(
            collected=len(alvos), failures=falhas + len(escuros),
            error=_erro_total_de_endpoints(escuros, len(endpoints)),
            extra={"eventos": eventos, "endpoints_escuros": escuros},
        )

    async def _alvos(self, endpoints: list[dict]) -> tuple[list[tuple[int, dict]], list[int]]:
        """Containers rodando, em rodizio: cada ciclo pega a proxima fatia."""
        todos: list[tuple[int, dict]] = []
        escuros: list[int] = []
        for endpoint in endpoints:
            try:
                rows = await self.client.list_containers(int(endpoint["Id"]))
            except PortainerApiError:
                escuros.append(int(endpoint["Id"]))
                continue
            todos += [(int(endpoint["Id"]), r) for r in rows if (r.get("State") or "").lower() == "running"]
        todos.sort(key=lambda par: (par[0], _nome(par[1])))
        if len(todos) <= self.cfg.max_containers_por_ciclo:
            return todos, escuros
        inicio = self._vez % len(todos)
        self._vez = inicio + self.cfg.max_containers_por_ciclo
        return (todos + todos)[inicio:inicio + self.cfg.max_containers_por_ciclo], escuros

    async def _container(
        self, endpoint_id: int, row: dict, cursores: dict[str, float], stacks: dict, workflows: dict[str, str],
    ) -> list[EventRecord]:
        nome, chave = _nome(row), f"{endpoint_id}:{_nome(row)}"
        desde = cursores.get(chave)
        texto = await self.client.container_logs(
            endpoint_id, str(row["Id"]), tail=SEMENTE_LINHAS if desde is None else MAX_LINHAS,
            since=int(desde) if desde is not None else None, timestamps=True,
        )
        leitura = self._ler(nome, texto.splitlines(), desde)
        cursores[chave] = _novo_cursor(leitura.ate, desde)
        if desde is None:
            self._registrar_no_catalogo(nome, leitura, alertar=False, dono={})
            return []
        dono = dono_da_stack(stack_do_container(row), stacks)
        return self._registrar_no_catalogo(nome, leitura, alertar=True, dono=dono) + self._triggers_falhos(
            leitura, workflows)

    def _ler(self, nome: str, linhas: list[str], desde: float | None) -> Leitura:
        leitura = Leitura()
        eh_n8n = nome == self.cfg.container_n8n
        for linha in linhas:
            instante, texto = separar(linha)
            if desde is not None and instante is not None and instante.timestamp() <= desde:
                continue
            leitura.ate = max(filter(None, (leitura.ate, instante)), default=None)
            if eh_n8n and (trigger := falha_de_trigger(texto)) and instante:
                leitura.triggers.append((*trigger, instante))
            elif texto and eh_erro(texto) and not self._ignorado(nome, texto):
                sig = assinatura(texto)
                leitura.contagem[sig] += 1
                leitura.amostras[sig] = redact_text(texto)[:MAX_AMOSTRA]
                leitura.linhas.setdefault(sig, []).append(redact_text(texto))
        return leitura

    def _ignorado(self, nome: str, texto: str) -> bool:
        padroes = self._ignorar.get(nome, []) + self._ignorar.get("*", [])
        return any(p.search(texto) for p in padroes)

    def _registrar_no_catalogo(self, nome: str, leitura: Leitura, *, alertar: bool, dono: dict) -> list[EventRecord]:
        """Atualiza o catalogo e devolve no maximo UM evento para o container."""
        achados: list[tuple[str, int, str, str]] = []
        catalogo = self._carregar_catalogo()
        for sig, n in leitura.contagem.items():
            conhecido = catalogo.get(f"{nome}:{sig}")
            motivo = self._motivo(conhecido, n)
            self._atualizar(nome, sig, leitura.amostras[sig], n, conhecido)
            if alertar and motivo:
                achados.append((sig, n, leitura.amostras[sig], motivo))
        if not achados:
            return []
        logs = "\n".join(linha for sig, *_ in achados for linha in leitura.linhas[sig])[-MAX_LOG_NO_ALERTA:]
        return [evento_log_de_erro(nome, achados, logs, dono)]

    def _motivo(self, conhecido: dict | None, n: int) -> str | None:
        if conhecido is None:
            return "erro novo"
        media = float(conhecido.get("media") or 0)
        if n >= max(self.cfg.pico_minimo, self.cfg.fator_de_pico * media):
            return f"pico de {n} ocorrencias (media {media:.1f} por leitura)"
        return None

    def _atualizar(self, nome: str, sig: str, amostra: str, n: int, conhecido: dict | None) -> None:
        anterior = conhecido or {}
        media = n if not conhecido else (1 - PESO_DA_MEDIA) * float(anterior.get("media") or 0) + PESO_DA_MEDIA * n
        meta = {"amostra": amostra, "media": round(media, 2), "total": int(anterior.get("total") or 0) + n}
        self._carregar_catalogo()[f"{nome}:{sig}"] = meta
        self.repo.upsert_resource(source=self.source, kind=KIND, external_id=f"{nome}:{sig}", name=nome, meta=meta)

    def _carregar_catalogo(self) -> dict[str, dict]:
        if self._catalogo is None:
            self._catalogo = {r["external_id"]: r.get("meta") or {} for r in self.repo.list_resources(self.source, KIND)}
        return self._catalogo

    def _triggers_falhos(self, leitura: Leitura, workflows: dict[str, str]) -> list[EventRecord]:
        """Um evento por workflow, com quantas falhas de gatilho houve na ultima hora."""
        ultimos: dict[str, tuple[str, str, str, datetime]] = {}
        for trigger, workflow, erro, quando in leitura.triggers:
            workflow_id = workflows.get(workflow) or workflow
            janela = [t for t in self._triggers.get(workflow_id, []) if quando - t <= JANELA_DO_TRIGGER]
            if not janela:
                self._inicio_trigger[workflow_id] = quando
            self._triggers[workflow_id] = [*janela, quando]
            ultimos[workflow_id] = (workflow, trigger, redact_text(erro), quando)
        return [
            evento_falha_de_trigger(wid, wf, trigger, erro, quando, len(self._triggers[wid]), self._inicio_trigger[wid])
            for wid, (wf, trigger, erro, quando) in ultimos.items()
        ]

    def _ler_cursores(self) -> dict[str, float]:
        estado = self.repo.get_cursor(self.source, SCOPE)
        return json.loads(estado.cursor) if estado and estado.cursor else {}

    def _gravar_cursores(self, cursores: dict[str, float]) -> None:
        self.repo.set_cursor(CursorState(
            source=self.source, scope=SCOPE, cursor=json.dumps(cursores), last_success_at=now_utc(),
        ))


def _novo_cursor(ate: datetime | None, desde: float | None) -> float:
    """Ultima linha lida; sem linha nova, mantem o anterior (ou comeca de agora na semente)."""
    if ate is not None:
        return ate.timestamp()
    return desde if desde is not None else now_utc().timestamp()


def _nome(row: dict) -> str:
    return (row.get("Names") or ["/?"])[0].lstrip("/")
