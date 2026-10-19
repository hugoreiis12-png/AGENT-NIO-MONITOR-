"""Eventos da leitura de logs. A decisao (severidade, notificar) vem do Agent."""

from __future__ import annotations

from datetime import datetime

from ...notify.templates import formatar_horas
from ...store.repository import EventRecord, new_event_id, now_utc


def evento_log_de_erro(
    container: str, sig: str, ocorrencias: int, amostra: str, motivo: str, logs: str, dono: dict,
) -> EventRecord:
    """Uma assinatura de erro por evento: a chave de dedup e (container, assinatura)."""
    return EventRecord(
        id=new_event_id(), ts=now_utc(), source="portainer", event_type="container_log_error", severity="medium",
        title=f"Erro no log de {container}: {motivo}", resource_id=f"{container}:{sig}",
        detail={
            "recurso": container, "workflow_nome": container, "assinatura": sig,
            "ocorrencias": ocorrencias, "erro": amostra, "logs": logs,
            "detalhe": f"{motivo.capitalize()}: {ocorrencias} linha(s) com esta mensagem desde a leitura anterior.",
            **dono,
        },
    )


def evento_falha_de_trigger(
    workflow_id: str, workflow: str, trigger: str, erro: str, quando: datetime, na_janela: int,
    inicio: datetime,
) -> EventRecord:
    """Falha de trigger do n8n (lida no log do container): nao gera execucao, entao so aparece aqui."""
    return EventRecord(
        id=new_event_id(), ts=now_utc(), source="n8n", event_type="workflow_trigger_failed", severity="high",
        title=f"Gatilho de {workflow} falhou {na_janela}x na ultima hora", resource_id=workflow_id,
        streak_count=na_janela,
        detail={
            "workflow_id": workflow_id, "workflow_nome": workflow, "workflow_name": workflow,
            "no": trigger, "erro": erro, "ocorrencias": na_janela,
            "ultima": formatar_horas(quando), "ultima_ts": quando.isoformat(), "incidente": inicio.isoformat(),
            "detalhe": f"O gatilho '{trigger}' falhou {na_janela} vez(es) na ultima hora. "
                       "Falha de gatilho nao gera execucao: o workflow simplesmente nao roda.",
        },
    )
