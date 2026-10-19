"""Leitura no a no das execucoes com SUCESSO: erro escondido e no do caminho normal que nao rodou.

A lista do n8n so diz success/error. Uma execucao verde pode ter no em erro
(`continueOnFail`) ou ter pulado etapas -- e e isso que este coletor le, execucao por
execucao, com o payload completo (~23 KB cada, ~114 por hora medidas em 19/10/2026).

Marca d'agua pelo id da execucao (crescente no n8n): so o que for maior que o ultimo
lido entra. A primeira passada so APRENDE o caminho, sem alertar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from ...store.repository import CursorState, EventRecord, Repository, now_utc
from ..base import CollectorResult
from .client import N8NApiError, N8NClient
from .node_baseline import NodeBaseline
from .node_path import erros_escondidos, nos_ativos, por_que_nao_rodou, resumo_do_caminho, run_data
from .path_events import evento_erro_escondido, evento_no_nao_rodou

log = structlog.get_logger(__name__)

MAX_POR_CICLO = 60
MODOS_IGNORADOS = frozenset({"manual"})


@dataclass(frozen=True)
class Analise:
    execucao_id: int
    inicio: datetime
    rodaram: frozenset[str]
    com_erro: frozenset[str]


def _inicio(raw: dict[str, Any]) -> datetime:
    texto = raw.get("startedAt")
    return datetime.fromisoformat(texto.replace("Z", "+00:00")).astimezone(UTC) if texto else now_utc()


class ExecutionPathCollector:
    source = "n8n"
    scope = "success-paths"

    def __init__(self, client: N8NClient, repo: Repository, *, max_por_ciclo: int = MAX_POR_CICLO) -> None:
        self.client, self.repo, self.max_por_ciclo = client, repo, max_por_ciclo
        self.baseline = NodeBaseline(repo)
        # Ultima execucao analisada por workflow: a reconferencia da fila le daqui.
        self.ultimo: dict[str, Analise] = {}
        # (workflow, tipo) -> inicio do incidente aberto: o mesmo problema alerta UMA vez.
        self._abertos: dict[tuple[str, str], datetime] = {}

    async def run(self, *, name_by_id: dict[str, str] | None = None) -> CollectorResult:
        state = self.repo.get_cursor(self.source, self.scope)
        marca = int(state.cursor) if state and state.cursor else None
        try:
            novas = await self._novas(marca)
        except N8NApiError as exc:
            return self._falhou(state, exc)
        self._encerrar_streaks(novas)
        lidas = novas[-self.max_por_ciclo:]
        if len(novas) > len(lidas):
            log.warning("execucoes_de_sucesso_sem_analise", puladas=len(novas) - len(lidas))
        eventos, falhas = await self._analisar_todas(lidas, name_by_id or {}, alertar=marca is not None)
        nova_marca = max((int(r["id"]) for r in novas), default=marca)
        self.repo.set_cursor(CursorState(
            source=self.source, scope=self.scope, cursor=str(nova_marca) if nova_marca else None,
            last_success_at=now_utc(), consecutive_failures=0,
        ))
        return CollectorResult(
            collected=len(lidas), failures=falhas,
            extra={"eventos": eventos, "puladas": len(novas) - len(lidas)},
        )

    def _encerrar_streaks(self, novas: list[dict[str, Any]]) -> None:
        """Sucesso DEPOIS da ultima falha encerra o incidente de falha do workflow (o proximo erro abre outro)."""
        for raw in novas:
            workflow_id = str(raw.get("workflowId") or "")
            streak = self.repo.get_streak(workflow_id)
            if streak and streak.last_at and _inicio(raw) > streak.last_at:
                self.repo.resolve_streak(workflow_id, _inicio(raw))

    def _incidente(self, workflow_id: str, tipo: str, presente: bool, inicio: datetime) -> str | None:
        if not presente:
            self._abertos.pop((workflow_id, tipo), None)
            return None
        return self._abertos.setdefault((workflow_id, tipo), inicio).isoformat()

    async def _novas(self, marca: int | None) -> list[dict[str, Any]]:
        """Execucoes de sucesso com id acima da marca, da mais antiga para a mais nova."""
        novas: list[dict[str, Any]] = []
        async for pagina in self.client.iter_executions_pages(status="success"):
            dados = pagina.get("data", [])
            novas.extend(r for r in dados if marca is None or int(r["id"]) > marca)
            if marca is None or any(int(r["id"]) <= marca for r in dados):
                break
        return sorted(novas, key=lambda r: int(r["id"]))

    async def _analisar_todas(
        self, lidas: list[dict[str, Any]], nomes: dict[str, str], *, alertar: bool,
    ) -> tuple[list[EventRecord], int]:
        eventos: list[EventRecord] = []
        falhas = 0
        for raw in lidas:
            if raw.get("mode") in MODOS_IGNORADOS:
                continue
            try:
                detalhe = await self.client.get_execution(raw["id"], include_data=True)
            except N8NApiError as exc:
                log.warning("execucao_de_sucesso_ilegivel", exec_id=raw.get("id"), erro=str(exc))
                falhas += 1
                continue
            novos = self.analisar(raw, detalhe, nomes)
            eventos.extend(novos if alertar else [])
        return eventos, falhas

    def analisar(self, raw: dict[str, Any], detalhe: dict[str, Any], nomes: dict[str, str]) -> list[EventRecord]:
        workflow_id = str(raw.get("workflowId") or "")
        workflow = detalhe.get("workflowData") or {}
        runs = run_data(detalhe)
        nome = nomes.get(workflow_id) or workflow.get("name") or workflow_id
        esperados = self.baseline.esperados(workflow_id, set(nos_ativos(workflow)))
        faltando = {no: por_que_nao_rodou(workflow, runs, no) for no in sorted(esperados - set(runs))}
        escondidos = erros_escondidos(workflow, runs)
        self.baseline.registrar(workflow_id, nome, set(runs))
        inicio, execucao_id = _inicio(raw), int(raw["id"])
        self.ultimo[workflow_id] = Analise(execucao_id, inicio, frozenset(runs), frozenset(escondidos))
        caminho = resumo_do_caminho(runs) if faltando or escondidos else None
        eventos = []
        if incidente := self._incidente(workflow_id, "erro", bool(escondidos), inicio):
            eventos.append(evento_erro_escondido(workflow_id, nome, execucao_id, inicio, escondidos, caminho, incidente))
        if incidente := self._incidente(workflow_id, "falta", bool(faltando), inicio):
            eventos.append(evento_no_nao_rodou(workflow_id, nome, execucao_id, inicio, faltando, caminho, incidente))
        return eventos

    def _falhou(self, state: CursorState | None, exc: Exception) -> CollectorResult:
        log.error("coleta_de_caminhos_falhou", erro=str(exc))
        self.repo.set_cursor(CursorState(
            source=self.source, scope=self.scope, cursor=state.cursor if state else None,
            last_success_at=None, consecutive_failures=(state.consecutive_failures if state else 0) + 1,
        ))
        return CollectorResult(error=str(exc))
