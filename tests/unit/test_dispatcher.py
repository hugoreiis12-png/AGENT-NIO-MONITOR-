"""Despachante: resolvido descarta, persistente envia uma vez, falha reagenda."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from agent_nio_monitor.notify.dispatcher import QueueDispatcher
from agent_nio_monitor.notify.mailer import DispatchResult
from agent_nio_monitor.store.queue import FAILED, InMemoryQueue
from agent_nio_monitor.store.repository import EventRecord

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
PRONTO = T0 + timedelta(seconds=40)


class ChecadorFixo:
    def __init__(self, motivo: str | None = None) -> None:
        self.motivo_fixo, self.chamadas = motivo, 0

    async def motivo(self, evento: EventRecord) -> str | None:
        self.chamadas += 1
        return self.motivo_fixo


class Envio:
    def __init__(self, *respostas: DispatchResult | Exception) -> None:
        self.respostas, self.enviados = list(respostas), []

    async def __call__(self, evento: EventRecord) -> DispatchResult:
        self.enviados.append(evento)
        resposta = self.respostas.pop(0) if self.respostas else DispatchResult(status="sent", channel="n8n_webhook")
        if isinstance(resposta, Exception):
            raise resposta
        return resposta


def _fila_com_um_alerta() -> InMemoryQueue:
    fila = InMemoryQueue()
    e = EventRecord(id=uuid4(), ts=T0, source="portainer", event_type="container_stopped", severity="critical",
                    title="parou", resource_id="abc", detail={"recurso": "worker"})
    fila.enqueue(e, ready_at=T0 + timedelta(seconds=35), renotify_minutes=720, now=T0)
    return fila


def _despachante(fila, checador, envio, **kw) -> QueueDispatcher:  # noqa: ANN001
    return QueueDispatcher(fila, checador, envio, **kw)


class TestReconferencia:
    async def test_problema_resolvido_e_descartado_sem_enviar(self) -> None:
        fila, envio = _fila_com_um_alerta(), Envio()
        resultado = await _despachante(fila, ChecadorFixo("container voltou a rodar"), envio).tick(PRONTO)
        assert resultado["descartados"] == 1 and envio.enviados == []
        assert fila.items[0].status == "discarded"
        assert fila.items[0].resolved_reason == "container voltou a rodar"

    async def test_problema_que_continua_e_enviado_uma_vez_so(self) -> None:
        fila, envio = _fila_com_um_alerta(), Envio()
        d = _despachante(fila, ChecadorFixo(None), envio)
        assert (await d.tick(PRONTO))["enviados"] == 1
        assert (await d.tick(PRONTO + timedelta(seconds=30)))["enviados"] == 0
        assert len(envio.enviados) == 1
        assert fila.items[0].status == "sent"

    async def test_antes_do_prazo_nada_acontece(self) -> None:
        fila, envio, checador = _fila_com_um_alerta(), Envio(), ChecadorFixo(None)
        await _despachante(fila, checador, envio).tick(T0 + timedelta(seconds=10))
        assert envio.enviados == [] and checador.chamadas == 0
        assert fila.items[0].status == "pending"

    async def test_evento_entregue_ao_envio_e_o_mesmo_que_entrou(self) -> None:
        fila, envio = _fila_com_um_alerta(), Envio()
        await _despachante(fila, ChecadorFixo(None), envio).tick(PRONTO)
        assert envio.enviados[0].title == "parou"
        assert envio.enviados[0].detail["recurso"] == "worker"


class TestFalhasDeEnvio:
    async def test_excecao_reagenda_com_espera(self) -> None:
        fila = _fila_com_um_alerta()
        resultado = await _despachante(fila, ChecadorFixo(None), Envio(RuntimeError("n8n fora"))).tick(PRONTO)
        assert resultado["reagendados"] == 1
        assert fila.items[0].status == "pending" and fila.items[0].attempts == 1
        assert fila.items[0].next_attempt_at == PRONTO + timedelta(seconds=30)

    async def test_depois_de_n_tentativas_desiste_e_marca_falha(self) -> None:
        fila = _fila_com_um_alerta()
        d = _despachante(fila, ChecadorFixo(None), Envio(*[RuntimeError("x")] * 5), max_tentativas=3)
        agora = PRONTO
        for _ in range(3):
            await d.tick(agora)
            agora += timedelta(minutes=10)
        assert fila.items[0].status == FAILED

    async def test_recuperou_na_segunda_tentativa_envia(self) -> None:
        fila = _fila_com_um_alerta()
        d = _despachante(fila, ChecadorFixo(None), Envio(RuntimeError("x")))
        await d.tick(PRONTO)
        resultado = await d.tick(PRONTO + timedelta(seconds=31))
        assert resultado["enviados"] == 1 and fila.items[0].status == "sent"

    async def test_sem_destinatario_nao_fica_tentando(self) -> None:
        fila = _fila_com_um_alerta()
        envio = Envio(DispatchResult(status="failed", channel="nenhuma", error="sem destinatario"))
        resultado = await _despachante(fila, ChecadorFixo(None), envio).tick(PRONTO)
        assert resultado["falhas"] == 1 and fila.items[0].status == FAILED

    async def test_webhook_falhando_reagenda_e_depois_desiste(self) -> None:
        fila = _fila_com_um_alerta()
        falha = DispatchResult(status="failed", channel="n8n_webhook", error="404", retryable=True)
        d = _despachante(fila, ChecadorFixo(None), Envio(*[falha] * 3), max_tentativas=3)
        assert (await d.tick(PRONTO))["reagendados"] == 1
        assert fila.items[0].status == "pending" and fila.items[0].attempts == 1
        agora = PRONTO
        for _ in range(2):
            agora += timedelta(minutes=10)
            await d.tick(agora)
        assert fila.items[0].status == FAILED

    async def test_regra_sem_notificacao_descarta(self) -> None:
        fila = _fila_com_um_alerta()
        envio = Envio(DispatchResult(status="suppressed", channel="nenhuma"))
        resultado = await _despachante(fila, ChecadorFixo(None), envio).tick(PRONTO)
        assert resultado["descartados"] == 1

    async def test_envio_para_log_conta_como_enviado(self) -> None:
        fila = _fila_com_um_alerta()
        envio = Envio(DispatchResult(status="sent_to_log", channel="stderr"))
        assert (await _despachante(fila, ChecadorFixo(None), envio).tick(PRONTO))["enviados"] == 1


class TestExpiracao:
    async def test_alerta_velho_demais_e_descartado_sem_enviar(self) -> None:
        fila, envio = _fila_com_um_alerta(), Envio()
        d = _despachante(fila, ChecadorFixo(None), envio, expira_em=timedelta(minutes=60))
        resultado = await d.tick(T0 + timedelta(minutes=90))
        assert resultado["descartados"] == 1 and envio.enviados == []
        assert "expirado" in fila.items[0].resolved_reason
