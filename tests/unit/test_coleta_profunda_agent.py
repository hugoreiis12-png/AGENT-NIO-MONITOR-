"""Coleta profunda ligada ao Agent: decisao pela policy, reconferencia na fila e texto do alerta."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_nio_monitor.agent import Agent
from agent_nio_monitor.config.loader import load
from agent_nio_monitor.notify import templates
from agent_nio_monitor.sources.n8n.execution_paths import Analise
from agent_nio_monitor.sources.n8n.path_events import evento_no_nao_rodou
from agent_nio_monitor.sources.portainer.log_events import evento_falha_de_trigger
from agent_nio_monitor.store.memory_repository import InMemoryRepository

T0 = datetime(2026, 10, 19, 12, tzinfo=UTC)


@pytest.fixture
async def agente():  # noqa: ANN201
    a = Agent(load(), InMemoryRepository())
    yield a
    await a.llm.aclose()
    await a.notifier.aclose()
    await a.n8n.aclose()


def _no_nao_rodou():  # noqa: ANN202
    return evento_no_nao_rodou("wf1", "Pedidos", 31, T0, {"Grava": "'IF' mandou os itens por outra saida"},
                               "1. Webhook (1 item(ns), 3 ms)", T0.isoformat())


class TestDecisao:
    async def test_gatilho_so_alerta_a_partir_de_3_falhas_na_hora(self, agente: Agent) -> None:
        duas = agente._decidir(evento_falha_de_trigger("wf1", "MB Email IMAP", "IMAP", "closed", T0, 2, T0))
        tres = agente._decidir(evento_falha_de_trigger("wf1", "MB Email IMAP", "IMAP", "closed", T0, 3, T0))
        assert duas.detail["notificar"] is False and duas.detail["motivo_supressao"] == "streak_abaixo_do_minimo"
        assert tres.detail["notificar"] is True and tres.severity == "high"

    async def test_etapa_que_nao_rodou_e_media_e_notifica(self, agente: Agent) -> None:
        evento = agente._decidir(_no_nao_rodou())
        assert evento.severity == "medium" and evento.detail["notificar"] is True


class TestReconferencia:
    async def test_descarta_quando_a_execucao_seguinte_rodou_os_nos(self, agente: Agent) -> None:
        agente.deep.caminhos.ultimo["wf1"] = Analise(32, T0 + timedelta(seconds=30), frozenset({"Grava"}), frozenset())
        motivo = await agente.pipeline.dispatcher.checker.motivo(_no_nao_rodou())
        assert motivo == "os nos voltaram a rodar na execucao seguinte"

    async def test_mantem_quando_nao_houve_execucao_depois(self, agente: Agent) -> None:
        agente.deep.caminhos.ultimo["wf1"] = Analise(31, T0, frozenset(), frozenset())
        assert await agente.pipeline.dispatcher.checker.motivo(_no_nao_rodou()) is None


class TestTexto:
    def test_alerta_em_texto_traz_a_causa_e_o_caminho(self) -> None:
        evento = _no_nao_rodou()
        assunto, corpo = templates.render("node_not_executed", event=evento, contexto=evento.detail)
        assert "etapa do fluxo nao rodou" in assunto
        assert "'IF' mandou os itens por outra saida" in corpo
        assert "1. Webhook" in corpo
