"""Despachante: concorrencia com teto e tempo maximo por item.

O defeito medido: `tick()` iterava os itens em SERIAL, com `await` dentro. Por
item, `motivo()` pode chamar a API do n8n e `dispatch()` espera o LLM (60 s)
alem de 3 tentativas de webhook. Com 50 itens prontos, um tick muito
ultrapassa o intervalo de 30 s da agenda -- a fila cresce em vez de drenar, e
os itens mais velhos expiram com o motivo errado ("agente ficou parado?"),
mandando o operador procurar a coisa errada.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta


from agent_nio_monitor.notify.dispatcher import (
    CONCORRENCIA,
    ESPERAS_SEGUNDOS,
    TIMEOUT_ITEM_SEGUNDOS,
    QueueDispatcher,
)
from agent_nio_monitor.notify.mailer import DispatchResult
from agent_nio_monitor.store.queue import InMemoryQueue
from agent_nio_monitor.store.repository import EventRecord

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _evento(recurso: str = "w", tipo: str = "container_stopped") -> EventRecord:
    from uuid import uuid4

    return EventRecord(
        id=uuid4(), ts=T0, source="portainer", event_type=tipo, severity="critical",
        title=f"{recurso} parou", resource_id=recurso, detail={},
    )


class _CheckerNuncaResolvido:
    """Nada se resolve: todo item pendente segue para o envio."""

    async def motivo(self, evento: EventRecord) -> str | None:
        return None


class TestSemHeadOfLine:
    async def test_item_lento_nao_atrasa_os_outros(self) -> None:
        """A prova de que o defeito era head-of-line e nao volume.

        O primeiro item dorme 1 s. Os outros 5 sao instantaneos. Em serial, o
        tick levaria ~1 s por item; com concorrencia, os 5 rapidos saem juntos
        e o tick inteiro fica perto do item mais lento.
        """
        fila = InMemoryQueue()
        lento = _evento("lento")
        rapidos = [_evento(f"rapido{i}") for i in range(5)]
        for e in [lento, *rapidos]:
            fila.enqueue(e, ready_at=T0, renotify_minutes=0, now=T0)

        async def dispatch(evento: EventRecord) -> DispatchResult:
            if evento.resource_id == "lento":
                await asyncio.sleep(1.0)
            return DispatchResult(status="sent", channel="teste")

        disp = QueueDispatcher(fila, _CheckerNuncaResolvido(), dispatch)
        t0 = asyncio.get_running_loop().time()
        contagem = await disp.tick(T0)
        decorrido = asyncio.get_running_loop().time() - t0

        assert contagem["enviados"] == 6
        assert decorrido < 2.0, (
            f"tick levou {decorrido:.2f} s. Com 1 item lento e 5 instantaneos, "
            "o serial levaria ~1 s por item -- Indica que a concorrencia nao "
            "está pegando."
        )

    async def test_o_teto_e_respeitado(self) -> None:
        """Sem teto, 50 itens viram 50 chamadas de LLM e 50 conexoes HTTP."""
        fila = InMemoryQueue()
        for i in range(12):
            fila.enqueue(_evento(f"w{i}"), ready_at=T0, renotify_minutes=0, now=T0)

        simultaneas = 0
        maximo = 0

        async def dispatch(evento: EventRecord) -> DispatchResult:
            nonlocal simultaneas, maximo
            simultaneas += 1
            maximo = max(maximo, simultaneas)
            await asyncio.sleep(0.05)
            simultaneas -= 1
            return DispatchResult(status="sent", channel="teste")

        await QueueDispatcher(fila, _CheckerNuncaResolvido(), dispatch).tick(T0)
        assert maximo <= CONCORRENCIA, f"{maximo} itens ao mesmo tempo, teto e {CONCORRENCIA}"

    async def test_o_teto_e_configuravel(self) -> None:
        fila = InMemoryQueue()
        for i in range(6):
            fila.enqueue(_evento(f"w{i}"), ready_at=T0, renotify_minutes=0, now=T0)

        simultaneas = 0
        maximo = 0

        async def dispatch(evento: EventRecord) -> DispatchResult:
            nonlocal simultaneas, maximo
            simultaneas += 1
            maximo = max(maximo, simultaneas)
            await asyncio.sleep(0.05)
            simultaneas -= 1
            return DispatchResult(status="sent", channel="teste")

        await QueueDispatcher(
            fila, _CheckerNuncaResolvido(), dispatch, concorrencia=2
        ).tick(T0)
        assert maximo <= 2, f"{maximo} itens com concorrencia=2"


class TestTempoMaximoPorItem:
    async def test_item_preso_nao_prende_o_tick(self) -> None:
        fila = InMemoryQueue()
        preso = _evento("preso")
        fila.enqueue(preso, ready_at=T0, renotify_minutes=0, now=T0)

        async def dispatch(evento: EventRecord) -> DispatchResult:
            await asyncio.Event().wait()  # nunca retorna
            raise AssertionError("nao deveria chegar aqui")

        disp = QueueDispatcher(
            fila, _CheckerNuncaResolvido(), dispatch, timeout_item=0.2
        )
        contagem = await disp.tick(T0)

        assert contagem["reagendados"] == 1, (
            f"um item preso tem que ser reagendado, nao segurar o tick: {contagem}"
        )
        assert fila.items[0].next_attempt_at is not None
        assert "timeout" in (fila.items[0].last_error or ""), (
            f"o motivo tem que dizer timeout: {fila.items[0].last_error!r}"
        )

    async def test_o_timeout_reaumenta_a_expectativa_dos_outros(self) -> None:
        """O item preso nao pode atrasar nem o item bom."""
        fila = InMemoryQueue()
        fila.enqueue(_evento("preso"), ready_at=T0, renotify_minutes=0, now=T0)
        fila.enqueue(_evento("bom"), ready_at=T0, renotify_minutes=0, now=T0)

        async def dispatch(evento: EventRecord) -> DispatchResult:
            if evento.resource_id == "preso":
                await asyncio.Event().wait()
            return DispatchResult(status="sent", channel="teste")

        contagem = await QueueDispatcher(
            fila, _CheckerNuncaResolvido(), dispatch, timeout_item=0.2
        ).tick(T0)

        assert contagem["enviados"] == 1, "o item bom nao podia esperar pelo preso"
        assert contagem["reagendados"] == 1

    def test_o_teto_padrao_cobre_o_pior_caso_de_um_item(self) -> None:
        """Cortar antes de o item terminar de trabalhar transforma lentidao em
        falha definitiva -- o pior resultado para um alerta."""
        pior_do_envio = 3 * 15.0  # as 3 tentativas do webhook, timeout 15 s cada
        assert pior_do_envio < TIMEOUT_ITEM_SEGUNDOS, (
            f"tempo maximo de {TIMEOUT_ITEM_SEGUNDOS:.0f}s e menor que o pior "
            f"caso de envio ({pior_do_envio:.0f}s): um item lento seria cortado "
            "e marcado como falha enquanto ainda estava trabalhando"
        )

    def test_o_tempo_maximo_e_maior_que_o_intervalo_da_fila(self) -> None:
        """Se fosse menor que o ciclo, um item saudavel seria cortado em todo
        tique -- e viraria falha, nao lentidao."""
        from agent_nio_monitor.config.loader import load

        ciclo = load().polling.queue_seconds
        assert ciclo < TIMEOUT_ITEM_SEGUNDOS, (
            f"tempo maximo de {TIMEOUT_ITEM_SEGUNDOS:.0f}s e menor que o ciclo "
            f"da fila ({ciclo:.0f}s)"
        )


class TestItemQueLevanta:
    async def test_um_item_que_levanta_nao_derruba_os_outros(self) -> None:
        """`return_exceptions=True`: o comportamento antigo era `contagem[resultado] += 1`
        direto no resultado de `await`, entao uma exception unexpected SUBIA e o
        tick inteiro parava -- os itens seguintes ficavam sem marcador nenhum,
       _DEVENDO_ o status `pending` e invisiveis."""
        fila = InMemoryQueue()
        fila.enqueue(_evento("explode"), ready_at=T0, renotify_minutes=0, now=T0)
        fila.enqueue(_evento("normal"), ready_at=T0, renotify_minutes=0, now=T0)

        async def dispatch(evento: EventRecord) -> DispatchResult:
            if evento.resource_id == "explode":
                raise ValueError("defeito inesperado")
            return DispatchResult(status="sent", channel="teste")

        contagem = await QueueDispatcher(fila, _CheckerNuncaResolvido(), dispatch).tick(T0)

        assert contagem["enviados"] == 1, "o item bom tem que sair"
        assert contagem["reagendados"] == 1, "o que explodiu tem que ser reagendado"

        for item in fila.items:
            assert item.next_attempt_at is not None, (
                f"{item.resource_id} ficou sem marcador nenhum"
            )


class TestExpiracao:
    async def test_o_motivo_diz_a_causa_certa(self) -> None:
        """O motivo antigo era fixo: "expirado (agente ficou parado?)".

        Ele descreve UM motivo possivel e nao o que o codigo mede. Com o
        head-of-line corrigido, o mais comum aqui e fila crescendo mais rapido
        do que drena -- e mandar o operador investigar o agente parado e
       Viewport para a causa errada.
        """
        fila = InMemoryQueue()
        evento = _evento("velho")
        fila.enqueue(evento, ready_at=T0, renotify_minutes=0, now=T0)

        async def dispatch(evento: EventRecord) -> DispatchResult:  # pragma: no cover
            raise AssertionError("expirado nao deve ser despachado")

        agora_ = T0 + timedelta(hours=2)
        contagem = await QueueDispatcher(
            fila, _CheckerNuncaResolvido(), dispatch, expira_em=timedelta(minutes=60)
        ).tick(agora_)

        assert contagem["descartados"] == 1
        motivo = fila.items[0].resolved_reason
        assert "agente ficou parado" not in motivo, (
            f"o motivo antigo aponta para a causa errada: {motivo!r}"
        )
        assert "120 min" in motivo, f"o motivo tem que dizer quanto tempo na fila: {motivo!r}"
        assert "60 min" in motivo, "o motivo tem que dizer o ttl"

    def test_backoff_cresce_e_tem_teto(self) -> None:
        assert list(ESPERAS_SEGUNDOS) == sorted(ESPERAS_SEGUNDOS)
        assert ESPERAS_SEGUNDOS[0] < ESPERAS_SEGUNDOS[-1], "o backoff precisa crescer"
        assert len(ESPERAS_SEGUNDOS) <= 6, "indice maior que isso estoura ESPERAS_SEGUNDOS"

