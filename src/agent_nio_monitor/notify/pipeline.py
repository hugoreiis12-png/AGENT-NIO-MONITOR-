"""Do evento ao disparo: grava, enfileira e, a cada ciclo, despacha o que ainda vale.

Nada e enviado na hora. O evento entra na fila e espera 1 ciclo da fonte; na
reconferencia, se o problema passou, o alerta e descartado; se continua, sai uma vez so.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta

import structlog

from ..config.schema import PollingConfig
from ..health import HealthState
from ..rules.engine import RuleEngine
from ..store.queue import ENFILEIRADO, EVENTOS_DE_ESTADO, NotificationQueue, agora
from ..store.repository import EventRecord, Repository
from .dispatcher import QueueDispatcher
from .mailer import DispatchResult
from .resolution import ResolutionChecker, espera_em_segundos

log = structlog.get_logger(__name__)

SEGUNDOS_POR_DIA = 86400


class EventPipeline:
    def __init__(
        self,
        *,
        repo: Repository,
        queue: NotificationQueue,
        rules: RuleEngine,
        polling: PollingConfig,
        health: HealthState,
        checker: ResolutionChecker,
        dispatch: Callable[[EventRecord], Awaitable[DispatchResult]],
    ) -> None:
        self.repo, self.queue, self.rules, self.polling, self.health = repo, queue, rules, polling, health
        self.dispatcher = QueueDispatcher(queue, checker, dispatch)
        self._ticks = 0
        self._ticks_por_dia = int(SEGUNDOS_POR_DIA / polling.queue_seconds)

    def enqueue(self, eventos: list[EventRecord]) -> int:
        """Grava cada evento e enfileira os que devem notificar. Devolve quantos entraram."""
        agora_ = agora()
        entraram = 0
        for evento in eventos:
            self.repo.save_event(evento)
            if evento.detail.get("notificar") is False:
                log.info("evento_registrado_sem_notificar", evento=evento.title, motivo=evento.detail.get("motivo_supressao"))
                continue
            renotifica = self.rules.re_notify_minutes(evento.event_type) if evento.event_type in EVENTOS_DE_ESTADO else 0
            pronto = agora_ + timedelta(seconds=espera_em_segundos(evento.event_type, self.polling))
            if self.queue.enqueue(evento, ready_at=pronto, renotify_minutes=renotifica, now=agora_) == ENFILEIRADO:
                entraram += 1
        return entraram

    async def flush(self) -> dict[str, int]:
        if self._ticks % self._ticks_por_dia == 0:
            self.queue.purge(agora())
        self._ticks += 1
        resultado = await self.dispatcher.tick()
        self.health.registra_fila(self.queue.stats(agora()))
        return resultado
