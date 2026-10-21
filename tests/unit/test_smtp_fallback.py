"""Canal de reserva SMTP (ADR D46): so entra quando o webhook do n8n esgota; D8 e D45 intactos."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

import httpx
import pytest
import respx

from agent_nio_monitor.config.loader import load
from agent_nio_monitor.config.schema import MailConfig, SmtpConfig
from agent_nio_monitor.notify import smtp_fallback
from agent_nio_monitor.notify.mailer import DispatchResult, Notifier
from agent_nio_monitor.notify.smtp_fallback import SmtpFallback, build_smtp_fallback

URL = "http://n8n.test/webhook/x"
CFG = SmtpConfig(host="smtp.test", from_address="monitor@jfc.test")


class ReservaFalsa:
    def __init__(self, status: str = "sent") -> None:
        self.status, self.chamadas = status, 0

    async def send(self, **_: object) -> DispatchResult:
        self.chamadas += 1
        erro = None if self.status == "sent" else "recusado"
        return DispatchResult(status=self.status, channel="smtp_direto", error=erro)


class SmtpFalso:
    enviadas: list[EmailMessage] = []
    logins: list[str] = []

    def __init__(self, *_: object, **__: object) -> None:
        pass

    def __enter__(self) -> SmtpFalso:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def starttls(self) -> None:
        pass

    def login(self, usuario: str, _senha: str) -> None:
        SmtpFalso.logins.append(usuario)

    def send_message(self, mensagem: EmailMessage) -> dict:
        SmtpFalso.enviadas.append(mensagem)
        return {}


@pytest.fixture
def smtp_falso(monkeypatch: pytest.MonkeyPatch) -> type[SmtpFalso]:
    SmtpFalso.enviadas, SmtpFalso.logins = [], []
    monkeypatch.setattr(smtp_fallback.smtplib, "SMTP", SmtpFalso)
    monkeypatch.setattr(smtp_fallback.smtplib, "SMTP_SSL", SmtpFalso)
    return SmtpFalso


async def _enviar(reserva: ReservaFalsa | None, *, n8n_up: bool = True) -> DispatchResult:
    async with Notifier(webhook_url=URL, max_attempts=3, fallback=reserva) as n:
        return await n.send(recipients=["a@b.c"], subject="s", body="b", n8n_up=n8n_up)


class TestNotifierComReserva:
    @respx.mock
    async def test_webhook_esgotado_sai_por_smtp(self) -> None:
        respx.post(URL).mock(return_value=httpx.Response(500))
        reserva = ReservaFalsa()
        r = await _enviar(reserva)
        assert r.status == "sent" and r.channel == "smtp_direto" and "webhook falhou" in (r.error or "")
        assert reserva.chamadas == 1

    @respx.mock
    async def test_reserva_falhando_segue_retentavel(self) -> None:
        respx.post(URL).mock(side_effect=httpx.ConnectError("recusada"))
        r = await _enviar(ReservaFalsa(status="failed"))
        assert r.status == "failed" and r.retryable and r.channel == "n8n_webhook"
        assert "smtp: recusado" in (r.error or "")

    @respx.mock
    async def test_sem_reserva_comportamento_de_antes(self) -> None:
        respx.post(URL).mock(return_value=httpx.Response(404))
        r = await _enviar(None)
        assert r.status == "failed" and r.retryable and "smtp" not in (r.error or "")

    @respx.mock
    async def test_read_timeout_nao_aciona_reserva(self) -> None:
        respx.post(URL).mock(side_effect=httpx.ReadTimeout("lento"))
        reserva = ReservaFalsa()
        assert (await _enviar(reserva)).status == "sent" and reserva.chamadas == 0

    @respx.mock
    async def test_webhook_ok_nao_aciona_reserva(self) -> None:
        respx.post(URL).mock(return_value=httpx.Response(200))
        reserva = ReservaFalsa()
        assert (await _enviar(reserva)).channel == "n8n_webhook" and reserva.chamadas == 0

    async def test_n8n_fora_continua_indo_para_o_log(self) -> None:
        reserva = ReservaFalsa()
        assert (await _enviar(reserva, n8n_up=False)).status == "sent_to_log" and reserva.chamadas == 0


class TestSmtpFallback:
    async def test_envia_texto_e_html_com_login(self, smtp_falso: type[SmtpFalso]) -> None:
        r = await SmtpFallback(CFG, usuario="u", senha="p").send(
            recipients=["a@b.c", "d@e.f"], subject="s", body="b", html="<p>b</p>"
        )
        assert r.status == "sent" and r.channel == "smtp_direto"
        mensagem = smtp_falso.enviadas[0]
        assert [p.get_content_type() for p in mensagem.iter_parts()] == ["text/plain", "text/html"]
        assert mensagem["To"] == "a@b.c, d@e.f" and smtp_falso.logins == ["u"]

    async def test_sem_usuario_nao_faz_login(self, smtp_falso: type[SmtpFalso]) -> None:
        r = await SmtpFallback(CFG, usuario="", senha="").send(recipients=["a@b.c"], subject="s", body="b", html=None)
        assert r.status == "sent" and smtp_falso.logins == []

    async def test_servidor_fora_vira_falha_retentavel_sem_excecao(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _recusa(*_: object, **__: object) -> None:
            raise smtplib.SMTPConnectError(421, b"fora")

        monkeypatch.setattr(smtp_fallback.smtplib, "SMTP", _recusa)
        r = await SmtpFallback(CFG, usuario="", senha="").send(recipients=["a@b.c"], subject="s", body="b", html=None)
        assert r.status == "failed" and r.retryable and "SMTPConnectError" in (r.error or "")


class TestBuild:
    def test_sem_bloco_smtp_reserva_desligada(self) -> None:
        assert build_smtp_fallback(None) is None

    def test_usuario_sem_senha_desliga(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMTP_USERNAME", "u")
        monkeypatch.delenv("SMTP_PASSWORD", raising=False)
        assert build_smtp_fallback(CFG) is None

    def test_reserva_e_opcional_na_config(self) -> None:
        assert MailConfig(webhook_url="http://x").smtp is None

    def test_config_versionada_carrega_o_smtp_dentro_de_mail(self) -> None:
        smtp = load().mail.smtp
        assert smtp is not None
        # Porta 465 e TLS implicito: com starttls o cliente espera banner em texto puro ate o timeout.
        assert not (smtp.port == 465 and smtp.seguranca != "ssl")
