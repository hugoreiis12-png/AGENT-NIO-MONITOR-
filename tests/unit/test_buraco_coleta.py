"""Buraco de reconciliacao: a fiae que nunca existiu.

D7 dizia que o cursor invalidado pelo prune do n8n vira evento. Virou regra
(`config/policy.yaml`), template (`notify/templates.py`) e ADR. NUNCA virou
emissor -- o dado ia para `reconciliation_run` e para
`health.gaps_detected`, e o perda de historico nao chegava a ninguem.

Estes testes atravessam as 4 camadas que o evento precisa atravessar: o
coletor expoe os numeros, o Agent emite, a fila deduplica, o template
renderiza. Qualquer elo quebrado aqui e o alerta que nao sai.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from agent_nio_monitor.config.loader import load
from agent_nio_monitor.main import Agent
from agent_nio_monitor.notify import templates
from agent_nio_monitor.store.queue import EVENTOS_DE_ESTADO, chave_de_dedup
from agent_nio_monitor.store.repository import EventRecord
from agent_nio_monitor.store.memory_repository import InMemoryRepository

CURSOR_MORTA = "CURSOR_MORTA"
CURSOR_NOVA = "CURSOR_NOVA"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N8N_API_KEY", "k")
    monkeypatch.setenv("DATABASE_DSN", "postgresql://u:s@localhost/h")


def _pagina(dados: list[dict], proximo: str | None = None) -> dict:
    return {"data": dados, "nextCursor": proximo}


def _rota_que_responde_400_na_cursor(
    respx_mock, base: str, *, apos_reset: list[dict], proximo: str | None = None
) -> None:
    """400 na chamada com cursor (o cursor morreu), 200 na varredura do zero.

    `proximo` precisa ser None por padrao: com cursor de retorno, `_iter_pages`
    faz uma segunda chamada -- que esta rota responde com 400 de novo, e a
    varredura de recuperacao falha antes de devolver qualquer coisa.
    """

    def _handler(req) -> httpx.Response:  # noqa: ANN001
        if req.url.params.get("cursor"):
            return httpx.Response(400, json={"message": "Invalid pagination cursor supplied"})
        return httpx.Response(200, json=_pagina(apos_reset, proximo))

    respx_mock.get(f"{base}/api/v1/executions").mock(side_effect=_handler)


async def _agent_com_cursor_morta(respx_mock) -> Agent:  # noqa: ANN202
    cfg = load()
    respx_mock.get(f"{cfg.active.n8n.url}/api/v1/workflows").mock(
        return_value=httpx.Response(200, json={"data": [], "nextCursor": None})
    )
    repo = InMemoryRepository()
    from agent_nio_monitor.store.repository import CursorState

    repo.set_cursor(CursorState("n8n", "errors", cursor=CURSOR_MORTA))
    return Agent(cfg, repo)


class TestColetorExpoeOsNumeros:
    async def test_extra_traz_o_quanto_foi_perdido(self, respx_mock) -> None:
        """O alerta precisa dizer QUANTO, e essa informacao ja estava calculada
        para o `record_reconciliation` -- sair de graca, sem nova chamada a API."""
        from agent_nio_monitor.sources.n8n.client import N8NClient
        from agent_nio_monitor.sources.n8n.collector import ExecutionErrorCollector

        cfg = load()
        _rota_que_responde_400_na_cursor(respx_mock, cfg.active.n8n.url, apos_reset=[])
        repo = InMemoryRepository()
        from agent_nio_monitor.store.repository import CursorState

        repo.set_cursor(CursorState("n8n", "errors", cursor=CURSOR_MORTA))

        async with N8NClient(cfg.active.n8n.url, "k") as client:
            r = await ExecutionErrorCollector(client, repo, fetch_payload=False).run()

        rec = r.extra["reconciliation"]
        assert r.gap is True
        assert rec["gaps"] == 1
        assert rec["cursor_valido"] is False
        assert rec["esperados"] == 0  # varredura de recuperacao nao achou nada

    # A substituicao do cursor morto por um cursor de retorno e coberta em
    # tests/unit/test_collector.py::test_cursor_invalido_registra_buraco. Nao e
    # replicada aqui.


class TestAgenteEmite:
    async def test_buraco_vira_evento(self, respx_mock) -> None:
        agent = await _agent_com_cursor_morta(respx_mock)
        _rota_que_responde_400_na_cursor(respx_mock, agent.cfg.active.n8n.url, apos_reset=[])

        eventos = await agent.collect_execution_errors()
        try:
            buracos = [e for e in eventos if e.event_type == "reconciliation_gap"]
            assert len(buracos) == 1, f"esperado 1 evento de buraco, veio {len(buracos)}"
            evento = buracos[0]
            # O scope do coletor estabiliza a chave: dois buracos no mesmo
            # ciclo de despacho viram UM alerta, nao dois e-mails.
            assert evento.resource_id == "errors"
            assert evento.detail["notificar"] is True, "a regra manda notificar"
            assert evento.detail["template"] == "reconciliation_gap"
            assert "podou" in evento.detail["detalhe"]
        finally:
            await _fecha(agent)

    async def test_buraco_sozinho_sai_mesmo_sem_execucao_nova(self, respx_mock) -> None:
        """O caso que o early-return apagava: `collected == 0` depois do reset.

        A varredura de recuperacao nao achou nenhuma execucao nova -- e ainda
        assim houve perda. Se o evento waited atras do `return`, a perda mais
        silenciosa (a que ninguem nota) seria a que se perde.
        """
        agent = await _agent_com_cursor_morta(respx_mock)
        _rota_que_responde_400_na_cursor(respx_mock, agent.cfg.active.n8n.url, apos_reset=[])

        eventos = await agent.collect_execution_errors()
        try:
            assert [e.event_type for e in eventos] == ["reconciliation_gap"]
        finally:
            await _fecha(agent)

    async def test_sem_buraco_nao_emite(self, respx_mock) -> None:
        """Guarda contra o oposto: o alerta nao pode virar ruido em todo ciclo."""
        cfg = load()
        agent = await _agent_com_cursor_morta(respx_mock)
        respx_mock.get(f"{cfg.active.n8n.url}/api/v1/executions").mock(
            return_value=httpx.Response(200, json=_pagina([]))
        )

        eventos = await agent.collect_execution_errors()
        try:
            assert not [e for e in eventos if e.event_type == "reconciliation_gap"]
        finally:
            await _fecha(agent)

    async def test_contador_de_gaps_e_o_do_health(self, respx_mock) -> None:
        agent = await _agent_com_cursor_morta(respx_mock)
        _rota_que_responde_400_na_cursor(respx_mock, agent.cfg.active.n8n.url, apos_reset=[])

        antes = agent.health.gaps_detected
        await agent.collect_execution_errors()
        try:
            assert agent.health.gaps_detected == antes + 1
            assert agent.health.snapshot(timedelta(minutes=10))["gaps_detected"] == antes + 1
        finally:
            await _fecha(agent)


class TestFilaDeduplica:
    def test_buraco_e_estado_e_nao_fato(self) -> None:
        """Se fosse FATO, `chave_de_dedup` anexaria `evento.id` e dois buracos no
        mesmo ciclo de despacho viram dois e-mails -- a falha se repete por
        ciclos ate o prune do n8n ser resolvido."""
        assert "reconciliation_gap" in EVENTOS_DE_ESTADO

    def test_dois_buracos_geram_a_mesma_chave(self) -> None:
        instante = datetime(2026, 1, 1, tzinfo=UTC)
        a = EventRecord(id=uuid4(), ts=instante, source="n8n", event_type="reconciliation_gap",
                        severity="critical", title="buraco", resource_id="errors")
        b = EventRecord(id=uuid4(), ts=instante, source="n8n", event_type="reconciliation_gap",
                        severity="critical", title="buraco", resource_id="errors")
        assert chave_de_dedup(a) == chave_de_dedup(b)

    async def test_segundo_buraco_atualiza_o_pendente(self, respx_mock) -> None:
        from agent_nio_monitor.store.queue import agora

        cfg = load()
        agent = await _agent_com_cursor_morta(respx_mock)
        _rota_que_responde_400_na_cursor(respx_mock, cfg.active.n8n.url, apos_reset=[])

        try:
            fila = agent.pipeline.queue
            for _ in range(2):
                eventos = await agent.collect_execution_errors()
                await agent.process_events(eventos)
            pendentes = [
                i for i in fila.due(agora() + timedelta(hours=1))
                if i.event_type == "reconciliation_gap"
            ]
            assert len(pendentes) == 1, f"dois buracos viraram {len(pendentes)} pendentes"
        finally:
            await _fecha(agent)


class TestRender:
    def test_assunto_nao_resta_sufixo_quebrado(self) -> None:
        """O template dizia 'buraco de {min}min' e um evento de buraco nao tem
        `primeira`/`ultima` -- renderizava 'buraco de min'. O defeito era
        invisivel justamente porque o evento nunca existiu."""
        evento = EventRecord(
            id=uuid4(), ts=datetime(2026, 10, 14, 12, 0, tzinfo=UTC), source="n8n",
            event_type="reconciliation_gap", severity="critical",
            title="Buraco na coleta", resource_id="errors",
            detail={"detalhe": "o n8n podou execucoes"},
        )
        assunto, corpo = templates.render("reconciliation_gap", event=evento, contexto=evento.detail)

        assert "CRITICO" in assunto
        assert "min" not in assunto.replace("coleta", ""), f"assunto com sufixo quebrado: {assunto!r}"
        assert "podou" in corpo


async def _fecha(agent: Agent) -> None:
    await agent.llm.aclose()
    await agent.notifier.aclose()
    await agent.n8n.aclose()