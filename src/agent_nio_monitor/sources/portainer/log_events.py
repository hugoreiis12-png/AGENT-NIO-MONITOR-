"""Eventos da leitura de logs. A decisao (severidade, notificar) vem do Agent."""

from __future__ import annotations

from datetime import datetime

from ...notify.templates import formatar_horas
from ...store.repository import EventRecord, new_event_id, now_utc


def evento_log_de_erro(container: str, achados: list[tuple[str, int, str, str]], logs: str, dono: dict) -> EventRecord:
    """UM evento por container e leitura, com todas as mensagens novas ou em pico.

    `achados`: (assinatura, ocorrencias, amostra, motivo). Medido em 19/10/2026: um unico
    traceback de ForeignKeyViolation virou 13 assinaturas -- e 13 e-mails -- quando era um por
    assinatura. A chave de dedup e o container.
    """
    principal = max(achados, key=lambda a: a[1])
    total = sum(a[1] for a in achados)
    linhas = "\n".join(f"- {n}x [{motivo}] {amostra}" for _, n, amostra, motivo in achados)
    return EventRecord(
        id=new_event_id(), ts=now_utc(), source="portainer", event_type="container_log_error", severity="medium",
        title=f"Erro no log de {container}: {len(achados)} mensagem(ns) ({principal[3]})", resource_id=container,
        detail={
            "recurso": container, "workflow_nome": container, "assinaturas": [a[0] for a in achados],
            "ocorrencias": total, "erro": principal[2], "logs": logs,
            "detalhe": f"{total} linha(s) de erro desde a leitura anterior, em {len(achados)} mensagem(ns):\n{linhas}",
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
