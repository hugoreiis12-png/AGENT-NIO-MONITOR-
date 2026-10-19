"""Envio de um alerta ja decidido pela regra (o `dispatch` do `Agent`)."""

from __future__ import annotations

import structlog

from ..config.schema import AppConfig
from ..health import HealthState
from ..llm.analyst import AlertAnalyst
from ..store.repository import EventRecord, Repository, now_utc
from .compose import Mensagem, compor
from .mailer import DispatchResult, Notifier
from .resolver import RecipientResolver, Resolution

log = structlog.get_logger(__name__)

FONTES_DE_FALLBACK = ("people_yaml", "global_fallback", "nenhum")


async def enviar_alerta(
    evento: EventRecord, *, resolver: RecipientResolver, analyst: AlertAnalyst,
    notifier: Notifier, repo: Repository, health: HealthState, cfg: AppConfig,
) -> DispatchResult:
    """Do evento decidido ao e-mail: destinatario, texto (LLM ou template), envio e registro."""
    if not evento.detail.get("notificar", True):
        log.info(
            "evento_registrado_sem_notificar",
            evento=evento.title,
            motivo=evento.detail.get("motivo_supressao"),
        )
        return DispatchResult(status="suppressed", channel="nenhuma")

    destinatarios = await _destinatarios(evento, resolver, health)
    contexto = {
        **evento.detail, "tipo": evento.event_type, "fonte": evento.source,
        "titulo": evento.title, "severidade": evento.severity,
    }
    mensagem = await compor(analyst, evento, contexto, cfg)
    resultado = await notifier.send(
        recipients=destinatarios.emails,
        subject=mensagem.assunto,
        body=mensagem.corpo,
        html=mensagem.html,
        # Sinal real de queda (D8): n8n fora -> log, e nao e-mail.
        n8n_up=health.n8n_up is not False,
        severity=evento.severity,
        metadata={"event_id": str(evento.id), "streak": evento.streak_count, "llm": mensagem.status_llm},
    )
    _registrar(repo, evento, mensagem, resultado)
    return resultado


async def _destinatarios(evento: EventRecord, resolver: RecipientResolver, health: HealthState) -> Resolution:
    destinatarios = await resolver.resolve_for_event(evento.tenant_id, evento.detail, fonte=evento.source)
    if destinatarios.source in FONTES_DE_FALLBACK:
        log.warning("destinatario_por_fallback", fonte=destinatarios.source, detalhe=destinatarios.detail)
    if not destinatarios.emails:
        # Sem destinatario o item vira `failed` em definitivo; o contador no /healthz tira isso do silencio.
        health.conta_alerta_perdido()
        log.error(
            "alerta_sem_destinatario_foi_perdido",
            evento=evento.title,
            fonte=destinatarios.source,
            detalhe=destinatarios.detail,
        )
    return destinatarios


def _registrar(repo: Repository, evento: EventRecord, mensagem: Mensagem, resultado: DispatchResult) -> None:
    repo.save_notification(
        event_id=evento.id,
        channel=resultado.channel,
        recipients=resultado.recipients,
        subject=mensagem.assunto,
        body=mensagem.corpo,
        status=resultado.status,
        error=resultado.error,
        attempts=resultado.attempts,
    )
    if resultado.status in ("sent", "sent_to_log") and evento.resource_id:
        repo.mark_streak_notified(evento.resource_id, now_utc())
