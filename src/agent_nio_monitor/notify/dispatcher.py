"""Despachante da fila: a cada ciclo reconfere o alerta pendente e so envia se continua.

Resolvido = descarta com o motivo. Persistente = envia UMA vez. Falha de envio
reagenda com espera crescente e desiste depois de N tentativas.

Os itens sao processados CONCORRENTES, com teto, e com tempo maximo por item.
O motivo esta medido: em serial, um item que espera o LLM (60 s) atrasa todos
os outros, o tick estoura o intervalo da fila e ela cresce em vez de drenar.
"""

from __future__ import annotations

import asyncio
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

# Teto de itens despachados ao MESMO tempo.
#
# Com iteracao serial, um item lento atrasa todos os outros: `motivo()` pode
# chamar a API do n8n, `dispatch()` espera o LLM (60 s) e ainda faz 3
# tentativas de webhook. Com 50 itens prontos, o tick muito ultrapassa o
# intervalo da fila e ela cresce em vez de drenar.
#
# Baixo de proposito. Acima de ~4 o ganho e pequeno e o custo e real: cada
# item em voo pode segurar uma chamada de LLM e uma conexao HTTP ao mesmo
# tempo. Fila de alerta nao e tarefa de alta vazao -- e um ser que nao pode
# ser ignorado.
CONCORRENCIA = 4

# Teto por item. Maior que a soma do pior caso de um item (investigacao de 75 s +
# timeout do LLM + as 3 tentativas do webhook) e menor que o lease da fila (300 s) para que o TEMPO DE RESPOSTA, e nao o timeout,
# seja o que manda: cortando antes, um item lento viraria falha definitiva
# enquanto ainda estava trabalhando.
TIMEOUT_ITEM_SEGUNDOS = 240.0


class QueueDispatcher:
    def __init__(
        self,
        queue: NotificationQueue,
        checker: ResolutionChecker,
        dispatch: Callable[[EventRecord], Awaitable[DispatchResult]],
        *,
        expira_em: timedelta = timedelta(minutes=60),
        max_tentativas: int = 5,
        concorrencia: int = CONCORRENCIA,
        timeout_item: float = TIMEOUT_ITEM_SEGUNDOS,
    ) -> None:
        self.queue, self.checker, self.dispatch = queue, checker, dispatch
        self.expira_em, self.max_tentativas = expira_em, max_tentativas
        self.concorrencia, self.timeout_item = concorrencia, timeout_item

    async def tick(self, now: datetime | None = None) -> dict[str, int]:
        now = now or agora()
        contagem = {"enviados": 0, "descartados": 0, "falhas": 0, "reagendados": 0}
        itens = self.queue.due(now)
        if not itens:
            return contagem

        # `return_exceptions=True`: um item que levanta NAO pode derrubar os
        # outros. `_processar` ja trata as falhas conhecidas; isto cobre a
        #exception inesperada, que antes derrubava o tick inteiro e deixava os
        # itens restantes sem nenhum marcador.
        semaforo = asyncio.Semaphore(self.concorrencia)

        async def _um(item: QueueItem) -> str:
            async with semaforo:
                try:
                    return await asyncio.wait_for(self._processar(item, now), self.timeout_item)
                except TimeoutError:
                    return self._reagendar(
                        item,
                        f"timeout de {self.timeout_item:.0f}s no processamento",
                        now,
                    )
                except Exception as exc:  # noqa: BLE001 - um item nao derruba o tick
                    log.error("item_da_fila_levantou", item=item.dedup_key, erro=str(exc))
                    return self._reagendar(item, f"{type(exc).__name__}: {exc}", now)

        for desfecho in await asyncio.gather(*(_um(i) for i in itens), return_exceptions=True):
            if isinstance(desfecho, BaseException):
                log.error("item_da_fila_inesperado", erro=repr(desfecho))
                contagem["falhas"] += 1
            else:
                contagem[desfecho] += 1
        return contagem

    async def _processar(self, item: QueueItem, now: datetime) -> str:
        if now - item.created_at > self.expira_em:
            # O motivo antigo era "expirado (agente ficou parado?)" -- que
            # descreve UM dos motivos, e nao o que este codigo mede. Com o
            # head-of-line corrigido, o mais comum aqui e a fila ter crescido
            # mais rapido do que drena, e dizer "agente ficou parado" manda o
            # operador procurar a coisa errada.
            self.queue.mark_discarded(
                item.id,
                f"expirado: {int((now - item.created_at).total_seconds() // 60)} min na fila, "
                f"sem ser despachado (ttl de {int(self.expira_em.total_seconds() // 60)} min)",
                now,
            )
            log.error(
                "alerta_expirou_na_fila",
                item=item.dedup_key,
                tipo=item.event_type,
                minutos_na_fila=int((now - item.created_at).total_seconds() // 60),
                tentativas=item.attempts,
            )
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
        if resultado.retryable:
            return self._reagendar(item, resultado.error or resultado.status, now)
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
