"""Liga o diff estrutural (T5) ao pipeline de evento.

Um ciclo so: a lista completa de workflows (11,2 MB) e lida uma vez por ciclo e dela
saem tudo -- workflow criado, ativado/desativado e alterado. Antes havia um ciclo
"rapido" de ativacao que lia os mesmos 11,2 MB e se sobrepunha ao diff.
"""

from __future__ import annotations

from typing import Any

import structlog

from ...store.repository import Change, EventRecord, Repository, new_event_id, now_utc
from ..base import CollectorResult
from .client import N8NApiError, N8NClient
from .diff import autoria_de, diff_workflows, resumo, risco_para_score

log = structlog.get_logger(__name__)


def _eh_toggle(mudanca: Change) -> bool:
    return mudanca.kind == "settings_changed" and mudanca.path == "active"


class WorkflowMonitor:
    source = "n8n"

    def __init__(self, client: N8NClient, repo: Repository) -> None:
        self.client = client
        self.repo = repo

    async def check_structural_diff(
        self, *, name_by_id: dict[str, str] | None = None
    ) -> CollectorResult:
        """Body completo, projecao, diff contra o ultimo snapshot.

        So grava snapshot novo quando o workflow e novo ou o diff achou mudanca: assim
        o `before` do ciclo seguinte e sempre o ultimo estado ja relatado, sem
        inchar o banco com 2 MB identicos a cada ciclo."""
        nomes = name_by_id or {}
        eventos: list[EventRecord] = []
        baseline = self.repo.has_workflow_snapshots()
        try:
            async for wf in self.client.iter_workflows():
                eventos.extend(self._processar_workflow(wf, nomes, baseline))
        except N8NApiError as exc:
            log.error("diff_estrutural_falhou", erro=str(exc))
            return CollectorResult(error=str(exc))

        return CollectorResult(collected=len(eventos), extra={"eventos": eventos})

    def _processar_workflow(
        self, workflow: dict[str, Any], nomes: dict[str, str], baseline: bool
    ) -> list[EventRecord]:
        workflow_id = str(workflow.get("id"))
        nome = str(workflow.get("name") or nomes.get(workflow_id) or workflow_id)
        ativo = bool(workflow.get("active", False))
        version_id = workflow.get("versionId")

        anterior = self.repo.last_workflow_snapshot(workflow_id)
        self.repo.upsert_resource(
            source=self.source, kind="workflow", external_id=workflow_id,
            name=nome, critical=False, meta={"active": ativo},
        )

        if anterior is None:
            # O snapshot vem DEPOIS do evento, nunca antes. Gravar primeiro
            # avanca o baseline do diff: se o evento falhasse de qualquer jeito
            # depois disso, o proximo ciclo compararia contra o estado NOVO e a
            # mudanca nunca mais seria detectada -- perda silenciosa.
            evento = self._evento_criado(workflow, workflow_id, nome) if baseline else None
            self.repo.save_workflow_snapshot(workflow_id, version_id, ativo, workflow)
            # Sem linha de base global e a primeira coleta; com ela, e workflow NOVO.
            return [evento] if evento is not None else []

        mudancas: list[Change] = diff_workflows(anterior["body"], workflow)
        if not mudancas:
            return []  # snapshot equivalente ao atual: gravar de novo so incharia o banco

        eventos = []
        if any(_eh_toggle(m) for m in mudancas):
            eventos.append(self._evento_toggle(workflow_id, nome, ativo, autoria_de(workflow)))
        estruturais = [m for m in mudancas if not _eh_toggle(m)]
        if estruturais:
            eventos.append(self._evento_alterado(workflow, workflow_id, nome, estruturais))

        # Ultima operacao de escrita do ciclo, com os eventos ja em maos.
        self.repo.save_workflow_snapshot(workflow_id, version_id, ativo, workflow)
        return eventos

    def _evento_toggle(self, workflow_id: str, nome: str, ativo: bool, autor: str | None) -> EventRecord:
        tipo = "workflow_activated" if ativo else "workflow_deactivated"
        return EventRecord(
            id=new_event_id(),
            ts=now_utc(),
            source=self.source,
            event_type=tipo,
            severity="low",
            title=f"{nome} foi {'ativado' if ativo else 'desativado'}",
            resource_id=workflow_id,
            detail={"workflow_id": workflow_id, "workflow_nome": nome, "recurso": nome, "ativo": ativo, "autor": autor},
        )

    def _evento_alterado(
        self, workflow: dict[str, Any], workflow_id: str, nome: str, mudancas: list[Change]
    ) -> EventRecord:
        pontos, fatores = risco_para_score(mudancas)
        return EventRecord(
            id=new_event_id(),
            ts=now_utc(),
            source=self.source,
            event_type="workflow_changed",
            severity="medium",
            title=f"{nome} foi alterado",
            resource_id=workflow_id,
            changes=mudancas,
            detail={
                "workflow_id": workflow_id,
                "workflow_nome": nome,
                "recurso": nome,
                "resumo_diff": resumo(mudancas),
                "autor": autoria_de(workflow),
                "risco_score": pontos,
                "risco_fatores": fatores,
            },
        )

    def _evento_criado(self, workflow: dict[str, Any], workflow_id: str, nome: str) -> EventRecord:
        nos = workflow.get("nodes") or []
        tipos = sorted({str(n.get("type", "?")).rsplit(".", 1)[-1] for n in nos})
        return EventRecord(
            id=new_event_id(),
            ts=now_utc(),
            source=self.source,
            event_type="workflow_created",
            severity="medium",
            title=f"{nome} foi criado",
            resource_id=workflow_id,
            detail={
                "workflow_id": workflow_id,
                "workflow_nome": nome,
                "recurso": nome,
                "resumo_diff": f"Workflow novo com {len(nos)} nodes ({', '.join(tipos[:8]) or 'vazio'}).",
                "autor": autoria_de(workflow),
            },
        )
