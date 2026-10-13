"""Coleta de execucoes com falha.

Destaques de decisao:
  - polling por CURSOR, nao por tempo: a versao em uso nao garante `startedAfter`
  - cursor invalida quando o n8n poda execucao antiga -> CursorInvalid vira
    evento `reconciliation_gap`, nao um log esquecido
  - `duration_ms` e derivado de stoppedAt - startedAt (o summary nao traz)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from ...redaction import redact
from ...store.repository import CursorState, ExecutionRecord, FailureRecord, Repository
from ...store.repository import now_utc
from ..base import CollectorResult
from .client import CursorInvalid, N8NApiError, N8NClient

log = structlog.get_logger(__name__)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class ExecutionErrorCollector:
    source = "n8n"
    scope = "errors"

    def __init__(self, client: N8NClient, repo: Repository, *, fetch_payload: bool = True):
        self.client = client
        self.repo = repo
        self.fetch_payload = fetch_payload

    async def run(self, *, name_by_id: dict[str, str] | None = None) -> CollectorResult:
        inicio = now_utc()
        state = self.repo.get_cursor(self.source, self.scope)
        cursor = state.cursor if state else None

        novos: list[ExecutionRecord] = []
        falhas: list[FailureRecord] = []
        vistos: set[int] = set()
        gap = False
        next_cursor: str | None = cursor

        try:
            async for page in self.client._iter_pages(
                "/api/v1/executions",
                {"status": "error", "includeData": "false"},
                cursor,
            ):
                next_cursor = page.get("nextCursor") or next_cursor
                for raw in page.get("data", []):
                    await self._process(raw, novos, falhas, vistos, name_by_id or {})
        except CursorInvalid as exc:
            # Cursor morreu. Perdeu o que estava entre o ultimo ponto e o prune.
            # Registra buraco e REINICIA a varredura do zero -- nunca "tenta de novo".
            gap = True
            log.warning("cursor_invalido_registrando_buraco", erro=str(exc))
            novos.clear()
            vistos.clear()
            falhas.clear()
            resultado_reset, cursor_reset = await self._varredura_completa(
                novos, falhas, vistos, name_by_id or {}
            )
            if resultado_reset is not None:
                return resultado_reset
            next_cursor = cursor_reset
        except N8NApiError as exc:
            log.error("coleta_execucoes_falhou", erro=str(exc))
            falhas_anteriores = state.consecutive_failures if state else 0
            self.repo.set_cursor(
                CursorState(
                    source=self.source,
                    scope=self.scope,
                    cursor=cursor,
                    last_success_at=None,
                    consecutive_failures=falhas_anteriores + 1,
                )
            )
            return CollectorResult(error=str(exc))

        if novos:
            self.repo.record_executions(novos)
        for f in falhas:
            self.repo.record_failure(f)

        # O streak mora AQUI, e nao no Agent. O coletor registra o que aconteceu;
        # quem decide o que fazer com isso e o Agent. Se o streak dependesse do
        # Agent, qualquer outro consumidor do coletor (relatorio, replay) veria
        # falha sem streak.
        streaks: dict[str, object] = {}
        for r in novos:
            streaks[r.workflow_id] = self.repo.bump_streak(
                r.workflow_id, r.started_at, r.tenant_id, r.workflow_name
            )

        self.repo.set_cursor(
            CursorState(
                source=self.source,
                scope=self.scope,
                cursor=next_cursor,
                last_success_at=now_utc(),
                consecutive_failures=0,
            )
        )

        self.repo.record_reconciliation(
            source=self.source,
            scope=self.scope,
            expected=len(vistos),
            seen=len(novos),
            gaps=1 if gap else 0,
            cursor_valid=not gap,
            duration_ms=int((now_utc() - inicio).total_seconds() * 1000),
        )

        return CollectorResult(
            collected=len(novos),
            failures=len(falhas),
            gap=gap,
            duration_ms=int((now_utc() - inicio).total_seconds() * 1000),
            extra={"streaks": streaks},
        )

    async def _varredura_completa(
        self,
        novos: list[ExecutionRecord],
        falhas: list[FailureRecord],
        vistos: set[int],
        names: dict[str, str],
    ) -> tuple[CollectorResult | None, str | None]:
        """Re varredura a partir do zero. Devolve (erro, proximo_cursor).

        O cursor de retorno e OBRIGATORIO: sem ele, o proximo poll repetiria a
        mesma cursor morta e o agente ficaria em laco de falha para sempre.
        """
        proximo: str | None = None
        try:
            async for page in self.client._iter_pages(
                "/api/v1/executions", {"status": "error", "includeData": "false"}, None
            ):
                proximo = page.get("nextCursor") or proximo
                for raw in page.get("data", []):
                    await self._process(raw, novos, falhas, vistos, names)
        except N8NApiError as exc:
            log.error("coleta_falhou_apos_reset", erro=str(exc))
            return CollectorResult(error=str(exc)), None
        return None, proximo

    async def _process(
        self,
        raw: dict[str, Any],
        novos: list[ExecutionRecord],
        falhas: list[FailureRecord],
        vistos: set[int],
        names: dict[str, str],
    ) -> None:
        try:
            exec_id = int(raw["id"])
        except (KeyError, TypeError, ValueError):
            log.warning("execucao_sem_id_ignorada", raw_keys=sorted(raw.keys()))
            return

        vistos.add(exec_id)
        if self.repo.execution_exists(exec_id):
            return

        started = _parse_dt(raw.get("startedAt")) or now_utc()
        record = ExecutionRecord(
            id=exec_id,
            started_at=started,
            workflow_id=str(raw.get("workflowId") or ""),
            status=str(raw.get("status") or "unknown"),
            stopped_at=_parse_dt(raw.get("stoppedAt")),
            mode=raw.get("mode"),
            workflow_name=names.get(str(raw.get("workflowId") or "")),
        )
        if not record.workflow_id:
            log.warning("execucao_sem_workflow_id_ignorada", exec_id=exec_id)
            return
        novos.append(record)

        # Payload so entra em caso de falha, e so se pedirem. Sucesso nao precisa.
        if self.fetch_payload:
            falhas.append(await self._build_failure(record))

    async def _build_failure(self, record: ExecutionRecord) -> FailureRecord:
        erro: str | None = None
        ultimo_no: str | None = None
        run_data: Any = None
        truncado = True
        try:
            detalhe = await self.client.get_execution(record.id, include_data=True)
            data = detalhe.get("data") or {}
            resultado = data.get("resultData") or {}
            erro = (detalhe.get("error") or {}).get("message") or data.get("error")
            ultimo_no = resultado.get("lastNodeExecuted")
            run_data = resultado.get("runData")
            truncado = bool(detalhe.get("dataTooLargeToDisplay", False))
        except N8NApiError as exc:
            # Falhar em buscar o payload NAO pode perder o evento principal.
            log.warning("payload_indisponivel", exec_id=record.id, erro=str(exc))
            erro = erro or f"payload indisponivel: {exc}"
        return FailureRecord(
            execution_id=record.id,
            started_at=record.started_at,
            workflow_id=record.workflow_id,
            error_msg=erro,
            last_node=ultimo_no,
            # Redigido AQUI, nao so no repositorio. O Postgres tambem redige
            # (defesa em profundidade), mas se a redacao dependesse de cada
            # implementacao de repositorio, um dia alguem esqueceria um e o
            # payload com senha entraria silenciosamente.
            run_data=redact(run_data) if run_data is not None else None,
            truncated=truncado,
        )

