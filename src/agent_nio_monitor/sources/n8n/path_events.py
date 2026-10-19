"""Eventos da leitura no a no de execucoes com SUCESSO. A decisao (severidade, notificar) vem do Agent."""

from __future__ import annotations

from datetime import datetime

from ...notify.templates import formatar_horas
from ...redaction import redact_text
from ...store.repository import EventRecord, new_event_id, now_utc


def _base(
    workflow_id: str, nome: str, execucao_id: int, inicio: datetime, caminho: str | None, incidente: str,
) -> dict:
    return {
        "incidente": incidente,
        "workflow_id": workflow_id, "workflow_nome": nome, "workflow_name": nome,
        "execucao_id": execucao_id, "ultima_ts": inicio.isoformat(), "ultima": formatar_horas(inicio),
        "caminho": redact_text(caminho) if caminho else None,
    }


def evento_no_nao_rodou(
    workflow_id: str, nome: str, execucao_id: int, inicio: datetime,
    motivos: dict[str, str], caminho: str | None, incidente: str,
) -> EventRecord:
    """No do caminho normal ausente numa execucao que terminou verde."""
    nos = sorted(motivos)
    return EventRecord(
        id=new_event_id(), ts=now_utc(), source="n8n", event_type="node_not_executed", severity="medium",
        title=f"{nome}: {len(nos)} no(s) do caminho normal nao rodaram", resource_id=workflow_id,
        detail={
            **_base(workflow_id, nome, execucao_id, inicio, caminho, incidente),
            "nos": nos, "no": ", ".join(nos),
            "detalhe": "\n".join(f"- {no}: {motivos[no]}" for no in nos),
            "erro": "A execucao terminou com sucesso, mas pulou etapas que costumam rodar.",
        },
    )


def evento_erro_escondido(
    workflow_id: str, nome: str, execucao_id: int, inicio: datetime,
    erros: dict[str, str], caminho: str | None, incidente: str,
) -> EventRecord:
    """No com erro numa execucao 'verde' (continueOnFail / onError: continue)."""
    nos = sorted(erros)
    return EventRecord(
        id=new_event_id(), ts=now_utc(), source="n8n", event_type="node_error_hidden", severity="high",
        title=f"{nome}: erro em {len(nos)} no(s) numa execucao marcada como sucesso", resource_id=workflow_id,
        detail={
            **_base(workflow_id, nome, execucao_id, inicio, caminho, incidente),
            "nos": nos, "no": ", ".join(nos),
            "erro": redact_text(" | ".join(f"{no}: {erros[no]}" for no in nos)),
            "detalhe": "O no esta configurado para seguir em erro, entao a execucao aparece como sucesso.",
        },
    )
