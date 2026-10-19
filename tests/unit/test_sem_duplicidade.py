"""Sem duplicidade de envio (ADR D43). Cada caso reproduz um padrao achado no banco de producao
em 19/10/2026: 7 e-mails de "foi alterado" do mesmo workflow em 4 h, e o mesmo RPA quebrado
reenviado 9 vezes (a cada 12 h de janela vencida)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import respx

from agent_nio_monitor.sources.base import ainda_em_edicao
from agent_nio_monitor.sources.n8n.client import N8NClient
from agent_nio_monitor.sources.n8n.execution_paths import ExecutionPathCollector
from agent_nio_monitor.sources.n8n.workflow_monitor import WorkflowMonitor
from agent_nio_monitor.store.memory_repository import InMemoryRepository
from agent_nio_monitor.store.queue import ENFILEIRADO, SUPRIMIDO, InMemoryQueue, janela_de_renotificacao
from agent_nio_monitor.store.repository import EventRecord

T0 = datetime(2026, 10, 19, 12, tzinfo=UTC)
N8N = "http://n8n.test"


def _falha(incidente: str | None) -> EventRecord:
    detail = {"incidente": incidente} if incidente else {}
    return EventRecord(id=uuid4(), ts=T0, source="powerautomate", event_type="desktop_flow_failed",
                       severity="high", title="RPA falhou", resource_id="pa:f1", detail=detail)


def _enfileirar_e_enviar(fila: InMemoryQueue, evento: EventRecord, quando: datetime) -> str:
    desfecho = fila.enqueue(evento, ready_at=quando, renotify_minutes=janela_de_renotificacao(evento, 720), now=quando)
    for item in fila.due(quando):
        fila.mark_sent(item.id, quando)
    return desfecho


class TestUmAlertaPorIncidente:
    def test_mesmo_incidente_nao_reenvia_nem_depois_de_12h(self) -> None:
        fila = InMemoryQueue()
        assert _enfileirar_e_enviar(fila, _falha("2026-10-01T10:00:00+00:00"), T0) == ENFILEIRADO
        for horas in (13, 26, 39, 72):
            assert _enfileirar_e_enviar(
                fila, _falha("2026-10-01T10:00:00+00:00"), T0 + timedelta(hours=horas)) == SUPRIMIDO

    def test_incidente_novo_alerta_de_novo(self) -> None:
        fila = InMemoryQueue()
        _enfileirar_e_enviar(fila, _falha("2026-10-01T10:00:00+00:00"), T0)
        assert _enfileirar_e_enviar(fila, _falha("2026-10-19T13:00:00+00:00"), T0 + timedelta(hours=1)) == ENFILEIRADO

    def test_evento_sem_incidente_mantem_a_janela_da_regra(self) -> None:
        assert janela_de_renotificacao(_falha(None), 720) == 720


class N8nComExecucoes:
    def __init__(self, execucoes: list[dict]) -> None:
        self.execucoes = execucoes

    async def iter_executions_pages(self, **_kw):  # noqa: ANN003, ANN201
        yield {"data": self.execucoes}

    async def get_execution(self, execucao_id, include_data=True) -> dict:  # noqa: ANN001
        return {"workflowData": {"nodes": []}, "data": {"resultData": {"runData": {}}}}


class TestStreakDoN8nEncerra:
    async def test_sucesso_depois_da_falha_encerra_o_incidente(self) -> None:
        repo = InMemoryRepository()
        repo.bump_streak("wf1", T0, None, "Pedidos")
        sucesso = {"id": "9", "workflowId": "wf1", "mode": "trigger", "startedAt": "2026-10-19T12:05:00Z"}
        await ExecutionPathCollector(N8nComExecucoes([sucesso]), repo).run()
        assert repo.get_streak("wf1") is None

    async def test_sucesso_antigo_lido_tarde_nao_encerra_falha_mais_nova(self) -> None:
        repo = InMemoryRepository()
        repo.bump_streak("wf1", T0, None, "Pedidos")
        antigo = {"id": "8", "workflowId": "wf1", "mode": "trigger", "startedAt": "2026-10-19T11:00:00Z"}
        await ExecutionPathCollector(N8nComExecucoes([antigo]), repo).run()
        assert repo.get_streak("wf1") is not None


def _wf(nos: list[dict], atualizado: datetime, ativo: bool = True) -> dict:
    return {"id": "1", "name": "MB Email IMAP", "active": ativo, "nodes": nos, "connections": {},
            "updatedAt": atualizado.isoformat().replace("+00:00", "Z")}


def _no(nome: str, tipo: str = "n8n-nodes-base.set", **extra) -> dict:
    return {"id": nome, "name": nome, "type": tipo, **extra}


class TestEdicaoConsolidada:
    def test_janela_de_edicao(self) -> None:
        agora = datetime.now(UTC)
        assert ainda_em_edicao(agora - timedelta(minutes=3), 10, agora)
        assert not ainda_em_edicao(agora - timedelta(minutes=11), 10, agora)
        assert not ainda_em_edicao(agora, 0, agora), "0 desliga a espera"

    @respx.mock
    async def test_varios_saves_viram_um_alerta_com_todas_as_mudancas(self) -> None:
        mon = WorkflowMonitor(N8NClient(N8N, "k", verify_ssl=False), InMemoryRepository(), quieto_minutos=10)
        rota = respx.get(f"{N8N}/api/v1/workflows")
        agora = datetime.now(UTC)
        rota.mock(return_value=httpx.Response(200, json={"data": [_wf([_no("A")], agora - timedelta(days=1))]}))
        await mon.check_structural_diff()
        for minuto, nos in ((2, [_no("A", onError="continueRegularOutput")]),
                            (1, [_no("A", onError="continueRegularOutput"), _no("B")])):
            rota.mock(return_value=httpx.Response(200, json={"data": [_wf(nos, agora - timedelta(minutes=minuto))]}))
            assert (await mon.check_structural_diff()).extra["eventos"] == [], "editando: espera"
        rota.mock(return_value=httpx.Response(200, json={"data": [
            _wf([_no("A", onError="continueRegularOutput"), _no("B")], agora - timedelta(minutes=15))]}))
        eventos = (await mon.check_structural_diff()).extra["eventos"]
        assert len(eventos) == 1 and eventos[0].event_type == "workflow_changed"
        assert {m.kind for m in eventos[0].changes} == {"node_changed_risky", "node_added"}

    @respx.mock
    async def test_desativar_nao_espera_a_janela(self) -> None:
        mon = WorkflowMonitor(N8NClient(N8N, "k", verify_ssl=False), InMemoryRepository(), quieto_minutos=10)
        rota = respx.get(f"{N8N}/api/v1/workflows")
        agora = datetime.now(UTC)
        rota.mock(return_value=httpx.Response(200, json={"data": [_wf([], agora - timedelta(days=1))]}))
        await mon.check_structural_diff()
        rota.mock(return_value=httpx.Response(200, json={"data": [_wf([], agora, ativo=False)]}))
        assert [e.event_type for e in (await mon.check_structural_diff()).extra["eventos"]] == ["workflow_deactivated"]


class TestWebhookLento:
    """Caso real de 19/10/2026: o webhook do n8n so responde depois do SMTP; 3 ReadTimeout + 1
    sucesso mandaram o mesmo 'Container parado: hfbrasil-preco' ate 4 vezes."""

    @respx.mock
    async def test_sem_resposta_no_prazo_e_entrega_presumida_sem_reenvio(self) -> None:
        from agent_nio_monitor.notify.mailer import Notifier

        rota = respx.post("http://n8n.test/webhook/x").mock(side_effect=httpx.ReadTimeout("lento"))
        async with Notifier(webhook_url="http://n8n.test/webhook/x", max_attempts=3) as n:
            r = await n.send(recipients=["a@b.c"], subject="s", body="b")
        assert rota.call_count == 1, "um POST que chegou nao pode ser repetido"
        assert r.status == "sent" and not r.retryable and "presumida" in (r.error or "")

    @respx.mock
    async def test_conexao_recusada_ainda_tenta_de_novo(self) -> None:
        from agent_nio_monitor.notify.mailer import Notifier

        rota = respx.post("http://n8n.test/webhook/x").mock(side_effect=httpx.ConnectError("recusada"))
        async with Notifier(webhook_url="http://n8n.test/webhook/x", max_attempts=3) as n:
            r = await n.send(recipients=["a@b.c"], subject="s", body="b")
        assert rota.call_count == 3 and r.status == "failed" and r.retryable
