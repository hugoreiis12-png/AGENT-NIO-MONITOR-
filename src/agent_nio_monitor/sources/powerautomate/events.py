"""Evento de falha de fluxo de desktop (RPA). A decisao chega pronta do `RuleEngine`.

Tipo proprio (`desktop_flow_failed`), e nao `execution_failed`: o texto de acao, a
reconferencia e o destinatario sao do Power Automate, nao do n8n.
"""

from __future__ import annotations

from ...notify.templates import formatar_horas
from ...rules.engine import Decision
from ...store.repository import EventRecord, Streak, new_event_id, now_utc
from .desktop import SessaoDesktop

TIPO = "desktop_flow_failed"


def _erro(sessao: SessaoDesktop) -> str | None:
    partes = [p for p in (sessao.erro_codigo, sessao.erro_mensagem) if p]
    return ": ".join(partes) or None


def _detalhe(sessao: SessaoDesktop) -> str:
    modo = f" ({sessao.modo})" if sessao.modo else ""
    return f"Maquina {sessao.maquina or '?'}{modo}; codigo {sessao.erro_codigo or 'sem codigo'}"


def evento_de_falha_desktop(streak: Streak, sessao: SessaoDesktop, decisao: Decision) -> EventRecord:
    """Um streak de falhas do mesmo fluxo de desktop vira UM evento (ADR D4)."""
    return EventRecord(
        id=new_event_id(),
        ts=now_utc(),
        source="powerautomate",
        event_type=TIPO,
        severity=decisao.severity,
        title=f"{sessao.fluxo_nome} falhou {streak.count}x",
        resource_id=sessao.chave,
        streak_count=streak.count,
        detail={
            "fluxo_id": sessao.fluxo_id,
            "sessao_id": sessao.id,
            "workflow_nome": sessao.fluxo_nome,
            "ocorrencias": streak.count,
            "ultima_falha_ts": sessao.concluida_em.isoformat(),
            "primeira": formatar_horas(streak.first_at),
            "ultima": formatar_horas(streak.last_at),
            "erro": _erro(sessao),
            "detalhe": _detalhe(sessao),
            "maquina": sessao.maquina,
            "modo": sessao.modo,
            "responsavel_fluxo": sessao.dono,
            "notificar": decisao.notify,
            "motivo_supressao": decisao.suppress_reason,
            "template": decisao.template,
        },
    )
