"""Despachante da fila: a cada ciclo reconfere o alerta pendente e so envia se continua.

Resolvido = descarta com o motivo. Persistente = envia UMA vez. Falha de envio
reagenda com espera crescente e desiste depois de N tentativas.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import structlog

from ..store.queue import NotificationQueue, QueueItem, agora, payload_para_evento
from ..store.repository import EventRecord
from .mailer import DispatchResult
from .resolution import ResolutionChecker

log = structlog.get_logger(__name__)

ENVIADOS = ("sent", "sent_to_log")
ESPERAS_SEGUNDOS = (30, 60, 120, 240)


class QueueDispatcher:
    def __init__(
        self,
        queue: NotificationQueue,
        checker: ResolutionChecker,
        dispatch: Callable[[EventRecord], Awaitable[DispatchResult]],
        *,
        expira_em: timedelta = timedelta(minutes=60),
        max_tentativas: int = 5,
    ) -> None:
        self.queue, self.checker, self.dispatch = queue, checker, dispatch
        self.expira_em, self.max_tentativas = expira_em, max_tentativas

    async def tick(self, now: datetime | None = None) -> dict[str, int]:
        now = now or agora()
        contagem = {"enviados": 0, "descartados": 0, "falhas": 0, "reagendados": 0}
        for item in self.queue.due(now):
            resultado = await self._processar(item, now)
            contagem[resultado] += 1
        return contagem

    async def _processar(self, item: QueueItem, now: datetime) -> str:
        if now - item.created_at > self.expira_em:
            self.queue.mark_discarded(item.id, "expirado (agente ficou parado?)", now)
            return "descartados"
        evento = payload_para_evento(item.payload)
        motivo = await self.checker.motivo(evento)
        if motivo is not None:
            log.info("alerta_descartado_resolvido", evento=evento.title, motivo=motivo)
            self.queue.mark_discarded(item.id, motivo, now)
            return "descartados"
        return await self._enviar(item, evento, now)

    async def _enviar(self, item: QueueItem, evento: EventRecord, now: datetime) -> str:
        try:
            resultado = await self.dispatch(evento)
        except Exception as exc:  # noqa: BLE001 - uma falha de envio nao derruba o laco da fila
            return self._reagendar(item, f"{type(exc).__name__}: {exc}", now)
        if resultado.status in ENVIADOS:
            self.queue.mark_sent(item.id, now)
            return "enviados"
        if resultado.status == "suppressed":
            self.queue.mark_discarded(item.id, "regra: sem notificacao", now)
            return "descartados"
        self.queue.mark_failed(item.id, resultado.error or resultado.status)
        return "falhas"

    def _reagendar(self, item: QueueItem, erro: str, now: datetime) -> str:
        log.error("envio_da_fila_falhou", evento=item.dedup_key, tentativa=item.attempts + 1, erro=erro)
        if item.attempts + 1 >= self.max_tentativas:
            self.queue.mark_failed(item.id, erro)
            return "falhas"
        espera = ESPERAS_SEGUNDOS[min(item.attempts, len(ESPERAS_SEGUNDOS) - 1)]
        self.queue.mark_retry(item.id, erro, now + timedelta(seconds=espera))
        return "reagendados"
