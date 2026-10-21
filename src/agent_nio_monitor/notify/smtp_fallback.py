"""Canal de reserva (ADR D46): SMTP direto quando o webhook de e-mail do n8n esgota."""

from __future__ import annotations

import asyncio
import os
import smtplib
from email.message import EmailMessage

import structlog

from ..config.schema import SmtpConfig
from .mailer import DispatchResult

log = structlog.get_logger(__name__)

CANAL = "smtp_direto"


class SmtpFallback:
    def __init__(self, cfg: SmtpConfig, *, usuario: str, senha: str) -> None:
        self.cfg, self._usuario, self._senha = cfg, usuario, senha

    async def send(self, *, recipients: list[str], subject: str, body: str, html: str | None) -> DispatchResult:
        mensagem = self._montar(recipients, subject, body, html)
        try:
            recusados = await asyncio.to_thread(self._enviar, mensagem)
        except (smtplib.SMTPException, OSError) as exc:
            erro = f"{type(exc).__name__}: {exc}"
            log.error("smtp_direto_falhou", erro=erro)
            return DispatchResult(
                status="failed", channel=CANAL, recipients=recipients, error=erro, attempts=1, retryable=True,
            )
        if recusados:
            log.warning("smtp_direto_recusou_parte", recusados=sorted(recusados))
        log.warning("notificacao_por_smtp_direto", qtd=len(recipients))
        return DispatchResult(status="sent", channel=CANAL, recipients=recipients, attempts=1)

    def _montar(self, recipients: list[str], subject: str, body: str, html: str | None) -> EmailMessage:
        mensagem = EmailMessage()
        mensagem["From"], mensagem["To"], mensagem["Subject"] = self.cfg.from_address, ", ".join(recipients), subject
        mensagem.set_content(body)
        if html:
            mensagem.add_alternative(html, subtype="html")
        return mensagem

    def _enviar(self, mensagem: EmailMessage) -> dict[str, tuple[int, bytes]]:
        classe = smtplib.SMTP_SSL if self.cfg.seguranca == "ssl" else smtplib.SMTP
        with classe(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout_seconds) as smtp:
            if self.cfg.seguranca == "starttls":
                smtp.starttls()
            if self._usuario:
                smtp.login(self._usuario, self._senha)
            return smtp.send_message(mensagem)


def build_smtp_fallback(cfg: SmtpConfig | None) -> SmtpFallback | None:
    """Sem bloco `mail.smtp` ou com usuario sem senha, a reserva fica desligada (e dito no log)."""
    if cfg is None:
        return None
    usuario, senha = os.environ.get(cfg.username_env, ""), os.environ.get(cfg.password_env, "")
    if usuario and not senha:
        log.warning("smtp_direto_desligado", motivo=f"{cfg.password_env} vazia")
        return None
    return SmtpFallback(cfg, usuario=usuario, senha=senha)