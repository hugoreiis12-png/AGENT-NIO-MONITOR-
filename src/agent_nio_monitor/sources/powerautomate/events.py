"""Evento de falha de fluxo de desktop (RPA). A decisao chega pronta do `RuleEngine`.

Tipo proprio (`desktop_flow_failed`), e nao `execution_failed`: o texto de acao, a
reconferencia e o destinatario sao do Power Automate, nao do n8n.
"""

from __future__ import annotations

from ...notify.templates import formatar_horas
from ...rules.engine import Decision
from ...store.repository import EventRecord, Streak, new_event_id, now_utc
from .desktop import SessaoDesktop
from .session_detail import AnaliseAcoes

TIPO = "desktop_flow_failed"


def _erro(sessao: SessaoDesktop) -> str | None:
    partes = [p for p in (sessao.erro_codigo, sessao.erro_mensagem) if p]
    return ": ".join(partes) or None


def _detalhe(sessao: SessaoDesktop, aprofundado: dict[str, str]) -> str:
    modo = f" ({sessao.modo})" if sessao.modo else ""
    linhas = [f"Maquina {sessao.maquina or '?'}{modo}; codigo {sessao.erro_codigo or 'sem codigo'}"]
    linhas += [f"{rotulo}: {aprofundado[chave]}" for chave, rotulo in ROTULOS if aprofundado.get(chave)]
    return "\n".join(linhas)


ROTULOS = (("explicacao", "Significado"), ("fase", "Parou na fase"), ("acao", "Acao que falhou"))


def evento_de_falha_desktop(
    streak: Streak, sessao: SessaoDesktop, decisao: Decision, aprofundado: dict[str, str] | None = None,
) -> EventRecord:
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
            "incidente": streak.first_at.isoformat() if streak.first_at else None,
            "primeira": formatar_horas(streak.first_at),
            "ultima": formatar_horas(streak.last_at),
            "erro": _erro(sessao),
            "detalhe": _detalhe(sessao, aprofundado or {}),
            **(aprofundado or {}),
            "maquina": sessao.maquina,
            "modo": sessao.modo,
            "responsavel_fluxo": sessao.dono,
            "notificar": decisao.notify,
            "motivo_supressao": decisao.suppress_reason,
            "template": decisao.template,
        },
    )


def evento_erros_tratados(sessao: SessaoDesktop, analise: AnaliseAcoes) -> EventRecord:
    """Sessao verde com acao que falhou dentro de tratamento de erro: o RPA seguiu, mas algo deu errado."""
    acoes = [a.descricao() for a in analise.tratadas]
    return EventRecord(
        id=new_event_id(), ts=now_utc(), source="powerautomate", event_type="desktop_flow_handled_errors",
        severity="medium", title=f"RPA {sessao.fluxo_nome} terminou com {len(acoes)} erro(s) tratado(s)",
        resource_id=sessao.chave,
        detail={
            "fluxo_id": sessao.fluxo_id, "sessao_id": sessao.id, "workflow_nome": sessao.fluxo_nome,
            "ocorrencias": len(acoes), "ultima": formatar_horas(sessao.concluida_em),
            "ultima_ts": sessao.concluida_em.isoformat(), "maquina": sessao.maquina,
            "responsavel_fluxo": sessao.dono, "acao": acoes[0] if acoes else None,
            "erro": "\n".join(acoes),
            "detalhe": f"A sessao terminou com SUCESSO, mas {len(acoes)} de {analise.total} acoes falharam "
                       "dentro de tratamento de erro (o fluxo seguiu em frente).",
        },
    )
