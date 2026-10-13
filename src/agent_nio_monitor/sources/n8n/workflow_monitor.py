"""Liga o diff estrutural (T5) ao pipeline de evento.

Dois ciclos, cadencias diferentes (ADR D6, ver `workflow_active_seconds` vs
`workflow_full_seconds`): o toggle de `active` e barato e serve de alerta
rapido; o diff estrutural completo exige o snapshot anterior e so roda no
ciclo lento, onde o custo da pagina de 11,2 MB ja seria pago mesmo assim.
"""

from __future__ import annotations

from typing import Any

import structlog

from ...store.repository import Change, EventRecord, Repository, new_event_id, now_utc
from ..base import CollectorResult
from .client import N8NApiError, N8NClient
from .diff import autoria_de, diff_workflows, resumo, risco_para_score

log = structlog.get_logger(__name__)


class WorkflowMonitor:
    source = "n8n"

    def __init__(self, client: N8NClient, repo: Repository) -> None:
        self.client = client
        self.repo = repo
        # Cache em memoria do ultimo `active` visto, por workflow_id. So serve
        # para o toggle RAPIDO (300s); o diff estrutural (900s) nao depende
        # disto -- a fonte de verdade dele e o snapshot persistido.
        self._active_cache: dict[str, bool] | None = None

    async def check_active_state(self, *, name_by_id: dict[str, str] | None = None) -> CollectorResult:
        """Ciclo rapido: so compara `active`. Primeira passada so povoa o cache
        -- sem isso, todo workflow existente pareceria "acabou de ativar" no
        boot do agente."""
        nomes = name_by_id or {}
        eventos: list[EventRecord] = []
        vistos: dict[str, bool] = {}
        primeira_passada = self._active_cache is None
        cache_anterior = self._active_cache or {}

        try:
            async for wf in self.client.iter_workflows():
                wid = str(wf.get("id"))
                ativo = bool(wf.get("active", False))
                vistos[wid] = ativo
                if primeira_passada:
                    continue
                anterior = cache_anterior.get(wid)
                if anterior is not None and anterior != ativo:
                    eventos.append(
                        self._evento_toggle(wid, nomes.get(wid, wid), ativo)
                    )
        except N8NApiError as exc:
            log.error("checagem_de_estado_de_workflow_falhou", erro=str(exc))
            return CollectorResult(error=str(exc))

        self._active_cache = vistos
        return CollectorResult(collected=len(eventos), extra={"eventos": eventos})

    def _evento_toggle(self, workflow_id: str, nome: str, ativo: bool) -> EventRecord:
        tipo = "workflow_activated" if ativo else "workflow_deactivated"
        return EventRecord(
            id=new_event_id(),
            ts=now_utc(),
            source=self.source,
            event_type=tipo,
            severity="low",
            title=f"{nome} foi {'ativado' if ativo else 'desativado'}",
            resource_id=workflow_id,
            detail={"workflow_id": workflow_id, "workflow_nome": nome, "recurso": nome},
        )

    async def check_structural_diff(
        self, *, name_by_id: dict[str, str] | None = None
    ) -> CollectorResult:
        """Ciclo lento: body completo, projecao, diff contra o ultimo snapshot.

        Sempre grava um snapshot novo, mudanca ou nao -- senao o ciclo seguinte
        comparar contra um `before` desatualizado (o que gera diff fantasma
        incluindo coisa que ja tinha sido relatada antes)."""
        nomes = name_by_id or {}
        eventos: list[EventRecord] = []
        baseline = self.repo.has_workflow_snapshots()
        try:
            async for wf in self.client.iter_workflows():
                evento = self._processar_workflow(wf, nomes, baseline)
                if evento is not None:
                    eventos.append(evento)
        except N8NApiError as exc:
            log.error("diff_estrutural_falhou", erro=str(exc))
            return CollectorResult(error=str(exc))

        return CollectorResult(collected=len(eventos), extra={"eventos": eventos})

    def _processar_workflow(
        self, workflow: dict[str, Any], nomes: dict[str, str], baseline: bool
    ) -> EventRecord | None:
        workflow_id = str(workflow.get("id"))
        nome = str(workflow.get("name") or nomes.get(workflow_id) or workflow_id)
        ativo = bool(workflow.get("active", False))
        version_id = workflow.get("versionId")

        anterior = self.repo.last_workflow_snapshot(workflow_id)
        self.repo.save_workflow_snapshot(workflow_id, version_id, ativo, workflow)
        self.repo.upsert_resource(
            source=self.source, kind="workflow", external_id=workflow_id,
            name=nome, critical=False, meta={"active": ativo},
        )

        if anterior is None:
            # Sem linha de base global e a primeira coleta; com ela, e workflow NOVO.
            return self._evento_criado(workflow, workflow_id, nome) if baseline else None

        mudancas: list[Change] = diff_workflows(anterior["body"], workflow)
        if not mudancas:
            return None

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
