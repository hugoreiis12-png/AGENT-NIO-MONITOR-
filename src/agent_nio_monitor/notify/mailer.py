"""Envio de notificacao.

REGRA DE CANAL (decisao explicita do dono):
  - n8n de pe  -> webhook do n8n
  - n8n fora    -> **log no terminal**. Sem email, porque o email dependia do
                  proprio sistema que caiu.

Consequencia que precisa estar escrita: nesse segundo caminho o alerta chega a
alguem so se ALGUEM estiver consumindo esse log. Por isso todo envio para log
usa um prefixo inequivoco e o runbook exige um consumidor. Sem consumidor, o
Hermes falha em silencio -- que e o pior modo de falha de um monitor.

Escalacao: crescente por conjunto de destinatarios e por "nao reconhecido em N
minutos". Reenviar o mesmo email 3x nao e escalacao, e treino para ignorar.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

LOG_PREFIX = "HERMES-ALERTA"


@dataclass
class DispatchResult:
    status: str  # 'sent' | 'sent_to_log' | 'failed' | 'suppressed'
    channel: str
    recipients: list[str] = field(default_factory=list)
    error: str | None = None
    attempts: int = 0


class Notifier:
    def __init__(
        self,
        *,
        webhook_url: str,
        timeout: float = 15.0,
        max_attempts: int = 3,
        verify_ssl: bool = False,
    ) -> None:
        self.webhook_url = webhook_url
        self.max_attempts = max_attempts
        self._client = httpx.AsyncClient(timeout=timeout, verify=verify_ssl)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Notifier:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def send(
        self,
        *,
        recipients: list[str],
        subject: str,
        body: str,
        n8n_up: bool = True,
        severity: str = "medium",
        metadata: dict[str, Any] | None = None,
    ) -> DispatchResult:
        if not recipients:
            log.error("notificacao_sem_destinatario", subject=subject)
            return DispatchResult(
                status="failed", channel="nenhuma", error="sem destinatario"
            )

        if not n8n_up:
            return self._to_log(recipients, subject, body, metadata)

        payload = {
            "to": recipients,
            "subject": subject,
            "body": body,
            "severity": severity,
            "headers": {"X-Priority": _priority(severity)},
            "metadata": metadata or {},
        }
        ultimo_erro: str | None = None
        for tentativa in range(1, self.max_attempts + 1):
            try:
                resp = await self._client.post(self.webhook_url, json=payload)
                resp.raise_for_status()
                log.info("notificacao_enviada", qtd=len(recipients), tentativa=tentativa)
                return DispatchResult(
                    status="sent", channel="n8n_webhook", recipients=recipients,
                    attempts=tentativa,
                )
            except httpx.HTTPError as exc:
                ultimo_erro = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "notificacao_falhou", tentativa=tentativa, erro=ultimo_erro
                )
        # Webhook pode ter falhado porque o proprio n8n esta fora. Nao descarta:
        # cai para log, que e o canal declarado para esse cenario.
        log.warning("webhook_indisponivel_caindo_para_log", erro=ultimo_erro)
        resultado = self._to_log(recipients, subject, body, metadata)
        resultado.attempts = self.max_attempts
        return resultado

    def _to_log(
        self, recipients: list[str], subject: str, body: str, metadata: dict | None
    ) -> DispatchResult:
        registro = {
            "ts": datetime.now().astimezone().isoformat(),
            "para": recipients,
            "assunto": subject,
            "corpo": body,
            "metadata": metadata or {},
        }
        linha = f"{LOG_PREFIX} {json.dumps(registro, ensure_ascii=False)}"
        # stderr: e o que sobrevive quando stdout esta num tty de um processo
        # que ninguem observa. E o canal declarado para n8n fora do ar.
        print(linha, file=sys.stderr, flush=True)
        log.warning(
            "notificacao_via_log",
            motivo="n8n_indisponivel",
            qtd_destinatarios=len(recipients),
            assunto=subject,
        )
        return DispatchResult(
            status="sent_to_log", channel="stderr", recipients=recipients
        )


def _priority(severity: str) -> str:
    return {
        "critical": "1",
        "high": "2",
        "medium": "3",
        "low": "4",
        "info": "5",
    }.get(severity, "3")


class Escalator:
    """Escalacao por conjunto crescente. Cada degrau e um destinatario novo,
    disparado por 'nao reconhecido em N minutos'."""

    def __init__(self, steps: list[int]) -> None:
        self.steps = sorted(steps)

    def due(self, *, notified_at: datetime | None, now: datetime) -> int | None:
        """Maior degrau JA vencido, ou None.

        Devolver o menor vence seria re-disparar o primeiro degrau para sempre
        quando a escalacao atrasa -- exatamente o 'resend' que o doc original
        propunha, que nao e escalacao e e treino para ignorar alerta.
        """
        if not self.steps or notified_at is None:
            return None
        decorrido = (now - notified_at).total_seconds() / 60
        vencidos = [s for s in self.steps if decorrido >= s]
        return max(vencidos) if vencidos else None

    def next_step(self, last_step: int | None, *, now: datetime, notified_at: datetime | None) -> int | None:
        if notified_at is None:
            return None
        decorrido = (now - notified_at).total_seconds() / 60
        candidatos = [s for s in self.steps if decorrido >= s and (last_step is None or s > last_step)]
        return min(candidatos) if candidatos else None