"""Coletor do app Bubble (comprover): erros de negocio, saude de workflows,
mudancas de schema e disponibilidade da API.

Fatos verificados (30/10/2026):
  - O Bubble NAO tem log de workflow nativo; a unica fonte de "log" sao os
    data types criados pelo app para registrar erros de negocio.
  - /meta (857 KB) lista todos os tipos e workflows, sem auth.
  - /obj/<type> sem auth retorna 200 (data API publica).
  - /wf/<name> sem auth retorna 401 (esperado); 5xx = incidente real.
  - Cursor do Bubble: inteiro crescente (nulo na primeira chamada).

Estrategia "tempo real":
  - Polling de alta frequencia (30 s) nas error data types para capturar novos registros.
  - Health check dos workflows expostos (chamar endpoint com body vazio, detectar 5xx).
  - Polling do /meta para detectar mudancas de schema (workflows adicionados/removidos).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from ...redaction import redact_text
from ...store.repository import EventRecord, Repository, new_event_id, now_utc
from ..base import CollectorResult
from .client import BubbleApiError, BubbleClient

log = structlog.get_logger(__name__)

# Types de erro monitorados (confirmados por investigacao no /meta)
DEFAULT_ERROR_TYPES: tuple[str, ...] = (
    "errodecadastro",
    "errodecadastroprodutores",
    "errodelançamentodepedido",
    "errodeseparação",
    "erronota",
    "erro_roteirização",
    "erro_telefone",
    "falhadeseparação",
    "log_de_regsitro",
)

MAX_ERRO = 600
LIMITE_REGISTROS = 20


class BubbleCollector:
    """Coletor unificado do app Bubble."""

    source = "bubble"
    DEFAULT_ERROR_TYPES: tuple[str, ...] = DEFAULT_ERROR_TYPES

    def __init__(
        self,
        client: BubbleClient,
        repo: Repository,
        *,
        error_types: tuple[str, ...] | list[str] = DEFAULT_ERROR_TYPES,
        workflow_check: bool = True,
    ) -> None:
        self.client = client
        self.repo = repo
        self.error_types = tuple(error_types)
        self.workflow_check = workflow_check
        # Estado entre ciclos: ultimo schema visto, IDs ja vistos por type,
        # workflows 5xx ja reportados (para dedupe entre ciclos)
        self._ultimo_meta: dict[str, Any] | None = None
        self._ids_vistos: dict[str, set[str]] = {}
        self._wf_falhas: dict[str, str] = {}  # workflow -> mensagem (5xx ativo)

    async def coletar(self) -> CollectorResult:
        inicio = time.monotonic()
        falhas = 0
        coletados = 0
        eventos: list[EventRecord] = []
        gap = False

        try:
            meta = await self.client.fetch_meta()
        except BubbleApiError as exc:
            falhas += 1
            eventos.append(self._evento_api_indisponivel(str(exc)))
            return CollectorResult(
                collected=0, failures=falhas, gap=False,
                duration_ms=int((time.monotonic() - inicio) * 1000),
                error=str(exc), extra={"eventos": eventos},
            )
        except Exception as exc:
            falhas += 1
            eventos.append(self._evento_api_indisponivel(f"{type(exc).__name__}: {exc}"))
            return CollectorResult(
                collected=0, failures=falhas, gap=False,
                duration_ms=int((time.monotonic() - inicio) * 1000),
                error=str(exc), extra={"eventos": eventos},
            )

        # 1. Schema diff
        eventos_meta = self._diff_schema(meta)
        eventos.extend(eventos_meta)
        coletados += 1
        self._ultimo_meta = meta

        # 2. Error types (novos registros)
        for tipo in self.error_types:
            try:
                evs = await self._coletar_tipo(tipo)
                eventos.extend(evs)
                coletados += 1
            except BubbleApiError as exc:
                falhas += 1
                log.warning("bubble_tipo_falha", tipo=tipo, erro=str(exc))
            except Exception as exc:
                falhas += 1
                log.warning("bubble_tipo_falha_inesperado", tipo=tipo, erro=str(exc))

        # 3. Workflow health (5xx)
        if self.workflow_check:
            try:
                evs_wf = await self._check_workflows(meta)
                eventos.extend(evs_wf)
                coletados += 1
            except Exception as exc:
                falhas += 1
                log.warning("bubble_workflow_check_falha", erro=str(exc))

        duration = int((time.monotonic() - inicio) * 1000)
        return CollectorResult(
            collected=coletados,
            failures=falhas,
            gap=gap,
            duration_ms=duration,
            extra={"eventos": eventos},
        )

    # ------------------------------------------------------------- privado

    def _evento_api_indisponivel(self, motivo: str) -> EventRecord:
        return EventRecord(
            id=new_event_id(),
            ts=now_utc(),
            source=self.source,
            event_type="bubble_api_unreachable",
            severity="error",
            title="API do Bubble indisponivel",
            resource_id="bubble_api",
            detail={"motivo": redact_text(motivo)[:MAX_ERRO], "notificar": True},
        )

    def _diff_schema(self, meta: dict[str, Any]) -> list[EventRecord]:
        """Detecta tipos ou workflows adicionados/removidos desde o ultimo ciclo."""
        if self._ultimo_meta is None:
            return []  # primeiro ciclo: linha de base
        eventos: list[EventRecord] = []
        tipos_ant = set(self._ultimo_meta.get("types", {}).keys())
        tipos_ats = set(meta.get("types", {}).keys())
        wfs_ant = set(w.get("endpoint", "") for w in self._ultimo_meta.get("post", []))
        wfs_ats = set(w.get("endpoint", "") for w in meta.get("post", []))

        novos_tipos = tipos_ats - tipos_ant
        removidos_tipos = tipos_ant - tipos_ats
        novos_wfs = wfs_ats - wfs_ant
        removidos_wfs = wfs_ant - wfs_ats

        if not (novos_tipos or removidos_tipos or novos_wfs or removidos_wfs):
            return []

        mudancas = []
        if novos_tipos:
            mudancas.append({"tipos_adicionados": sorted(novos_tipos)})
        if removidos_tipos:
            mudancas.append({"tipos_removidos": sorted(removidos_tipos)})
        if novos_wfs:
            mudancas.append({"workflows_adicionados": sorted(novos_wfs)})
        if removidos_wfs:
            mudancas.append({"workflows_removidos": sorted(removidos_wfs)})

        eventos.append(EventRecord(
            id=new_event_id(),
            ts=now_utc(),
            source=self.source,
            event_type="bubble_schema_changed",
            severity="info",
            title="Schema do app Bubble mudou",
            resource_id="bubble_schema",
            detail={
                "mudancas": mudancas,
                "novos_tipos": sorted(novos_tipos),
                "removidos_tipos": sorted(removidos_tipos),
                "novos_workflows": sorted(novos_wfs),
                "removidos_workflows": sorted(removidos_wfs),
                "notificar": True,
            },
        ))
        return eventos

    async def _coletar_tipo(self, tipo: str) -> list[EventRecord]:
        """Busca novos registros em um type de erro; dedupe por _id."""
        try:
            pagina = await self.client.fetch_type_page(tipo, cursor=0, limit=LIMITE_REGISTROS)
        except BubbleApiError as exc:
            # 404 = type nao existe mais; nao e erro de coleta
            if exc.status == 404:
                log.debug("bubble_tipo_404", tipo=tipo)
                return []
            raise

        resposta = pagina.get("response", {})
        resultados = resposta.get("results", [])
        ids_vistos = self._ids_vistos.setdefault(tipo, set())

        eventos: list[EventRecord] = []
        for reg in resultados:
            reg_id = reg.get("_id", "")
            if not reg_id or reg_id in ids_vistos:
                continue
            ids_vistos.add(reg_id)
            eventos.append(self._evento_erro_novo(tipo, reg))

        return eventos

    def _evento_erro_novo(self, tipo: str, registro: dict[str, Any]) -> EventRecord:
        # Extrai campos relevantes do registro
        descricao = (
            registro.get("descri\u00e7\u00e3o")
            or registro.get("motivo")
            or registro.get("descri__o_text")
            or redact_text(str(registro))[:300]
        )
        codigo = registro.get("c\u00f3digo") or registro.get("c_digo_text") or ""
        criado_em = registro.get("Created Date", "")
        cliente = registro.get("cliente", "")

        detail: dict[str, Any] = {
            "tipo": tipo,
            "registro_id": registro.get("_id", ""),
            "criado_em": criado_em,
            "notificar": True,
        }
        if codigo:
            detail["codigo"] = str(codigo)
        if cliente and isinstance(cliente, str):
            detail["cliente"] = cliente[:200]

        titulo = f"Novo registro em {tipo}"
        if descricao:
            titulo += f": {str(descricao)[:80]}"

        return EventRecord(
            id=new_event_id(),
            ts=now_utc(),
            source=self.source,
            event_type="bubble_error_new",
            severity="warning",
            title=titulo,
            resource_id=registro.get("_id", ""),
            detail=detail,
        )

    async def _check_workflows(self, meta: dict[str, Any]) -> list[EventRecord]:
        """Chama cada workflow POST e detecta 5xx (incidente real).

        4xx = falha esperada (auth/validacao) -- nao e incidente.
        5xx = erro interno do Bubble -- incidente.
        """
        eventos: list[EventRecord] = []
        workflows = meta.get("post", [])

        async def checar(nome: str) -> tuple[str, int, str | None] | None:
            try:
                status, msg = await self.client.check_workflow(nome)
                return (nome, status, msg) if status >= 500 else None
            except BubbleApiError:
                return None
            except Exception:
                return None

        # Concurrencia limitada para nao sobrecarregar
        nomes = [w.get("endpoint", "") for w in workflows if w.get("endpoint")]
        semaph = asyncio.Semaphore(10)

        async def _checar_limitado(n: str) -> tuple[str, int, str | None] | None:
            async with semaph:
                return await checar(n)

        resultados = await asyncio.gather(*[_checar_limitado(n) for n in nomes], return_exceptions=True)

        for res in resultados:
            if res is None or isinstance(res, Exception):
                continue
            nome, status, msg = res
            if nome not in self._wf_falhas:
                eventos.append(EventRecord(
                    id=new_event_id(),
                    ts=now_utc(),
                    source=self.source,
                    event_type="bubble_workflow_5xx",
                    severity="error",
                    title=f"Workflow \'{nome}\' retornou {status}",
                    resource_id=nome,
                    detail={
                        "status": status,
                        "mensagem": redact_text(msg or "")[:MAX_ERRO],
                        "notificar": True,
                    },
                ))
            self._wf_falhas[nome] = msg or ""

        # Nao limpar: se um workflow para de falhar, nao gera evento (silencioso)
        # Mas mantemos o estado para nao re-alertar se falhar de novo
        return eventos
