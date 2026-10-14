"""Construcao dos eventos do ciclo de execucoes do n8n.

So montagem: a DECISAO (severidade, notificar, template) chega pronta do
`RuleEngine`, e a transicao de estado do n8n fica no `Agent`.
"""

from __future__ import annotations

from typing import Any

from ...notify.templates import formatar_horas
from ...rules.engine import Decision
from ...store.repository import EventRecord, new_event_id, now_utc
from .collector import ExecutionErrorCollector
from .workflow_index import WorkflowIndex


def _campos_da_decisao(decisao: Decision) -> dict[str, Any]:
    return {
        "notificar": decisao.notify,
        "motivo_supressao": decisao.suppress_reason,
        "template": decisao.template,
    }


def evento_de_falha(
    workflow_id: str, streak: Any, falha: Any, decisao: Decision, index: WorkflowIndex
) -> EventRecord:
    """Um streak de falhas de um workflow vira UM evento (ADR D4)."""
    nome = getattr(streak, "workflow_name", None) or workflow_id
    return EventRecord(
        id=new_event_id(),
        ts=now_utc(),
        source="n8n",
        event_type="execution_failed",
        severity=decisao.severity,
        title=f"{nome} falhou {streak.count}x",
        tenant_id=index.projects.get(workflow_id),
        resource_id=workflow_id,
        streak_count=streak.count,
        detail={
            "workflow_id": workflow_id,
            "workflow_name": nome,
            "workflow_nome": nome,
            "ocorrencias": streak.count,
            "ultima_falha_ts": streak.last_at.isoformat(),
            "autor": index.authors.get(workflow_id),
            "erro": falha.error_msg if falha else None,
            "no": falha.last_node if falha else None,
            "primeira": formatar_horas(streak.first_at),
            "ultima": formatar_horas(streak.last_at),
            **_campos_da_decisao(decisao),
        },
    )


def evento_de_buraco(reconciliacao: dict[str, Any], decisao: Decision) -> EventRecord:
    """O n8n podou execucoes que a varredura ainda nao tinha lido (ADR D7)."""
    perdidos = reconciliacao.get("esperados", 0)
    return EventRecord(
        id=new_event_id(),
        ts=now_utc(),
        source="n8n",
        event_type="reconciliation_gap",
        severity=decisao.severity,
        title="Buraco na coleta de execucoes do n8n",
        # Scope do coletor: chave de dedup estavel, dois buracos no mesmo despacho viram UM alerta.
        resource_id=ExecutionErrorCollector.scope,
        detail={
            "recurso": "coleta de execucoes",
            "workflow_nome": "coleta de execucoes",
            "detalhe": (
                f"O n8n podou execucoes que ainda nao tinham sido lidas. "
                f"{perdidos} execucao(oes) esperada(s) na varredura de recuperacao. "
                "O que aconteceu nesse intervalo nao esta neste monitor."
            ),
            "esperados": perdidos,
            "vistos": reconciliacao.get("vistos"),
            **_campos_da_decisao(decisao),
        },
    )


def evento_de_queda(decisao: Decision) -> EventRecord:
    """`source_down`: rastro de "o n8n ficou fora em tal hora" (ADR D8)."""
    return EventRecord(
        id=new_event_id(),
        ts=now_utc(),
        source="n8n",
        event_type="source_down",
        severity=decisao.severity,
        title="n8n esta indisponivel",
        # Recurso estavel: a chave de dedup precisa nao variar por ciclo.
        resource_id="n8n",
        detail={
            "recurso": "n8n",
            "workflow_nome": "n8n (fonte de todas as coletas)",
            "detalhe": (
                "O n8n parou de responder. A coleta de execucoes e o "
                "diff de workflow param, e nenhum alerta pode sair por "
                "e-mail -- o webhook de envio e do proprio n8n."
            ),
            **_campos_da_decisao(decisao),
        },
    )
