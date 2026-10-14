"""`source_down`: o rastro de quando o n8n saiu.

Regra, template e ADR existiam. Nenhum emissor. A regra e `notify: false`, e
isso e CORRETO por um motivo estrutural que este arquivo fixa:

    o canal de envio e o webhook do proprio n8n.

Se o n8n esta fora, o canal esta fora junto. Nem com `notify: true` o alerta
sairia por e-mail -- ele simplesmente nunca chegaria a ninguem. O valor do
evento e o RASTREIO: a queda fica gravada em `event` e logada, para quando o
n8n voltar alguem conseguir responder "quando comecou?".

Por isso `EventPipeline.enqueue` pula o que tem `notificar` False: nao ha
despachante para consultar, e nao ha o que despachar.
"""

from __future__ import annotations

import pytest

from agent_nio_monitor.config.loader import load
from agent_nio_monitor.main import Agent
from agent_nio_monitor.store.memory_repository import InMemoryRepository

N8N = "http://n8n.test"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import agent_nio_monitor.config.loader as loader

    monkeypatch.setattr(loader, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("N8N_API_KEY", "k")
    monkeypatch.setenv("DATABASE_DSN", "postgresql://u:s@localhost/h")
    monkeypatch.delenv("PORTAINER_API_KEY", raising=False)


@pytest.fixture
async def agent() -> Agent:
    a = Agent(load(), InMemoryRepository())
    try:
        yield a
    finally:
        await a.llm.aclose()
        await a.notifier.aclose()
        await a.n8n.aclose()


class TestEmissor:
    def test_a_regra_diz_que_nao_notifica(self) -> None:
        """Fixa a decisao. Se alguem virar `notify: true`, este teste explica
        por que isso nao funcionaria -- e nao e configuracao esquecida."""
        regra = next(r for r in load().policy.rules if r.name == "n8n_indisponivel")
        assert regra.event_types == ["source_down"]
        assert regra.notify is False, (
            "o canal de envio e o webhook do proprio n8n; com o n8n fora, o "
            "e-mail nao sairia de qualquer jeito"
        )
        assert regra.severity == "critical"

    async def test_queda_gera_evento(self, agent: Agent) -> None:
        agent.health.mark_n8n(True)   # ainda de pe
        agent._n8n_era_up = True

        agent.health.mark_n8n(False)
        eventos = agent._detecta_queda_do_n8n()

        assert [e.event_type for e in eventos] == ["source_down"]
        evento = eventos[0]
        assert evento.severity == "critical"
        assert evento.resource_id == "n8n", "recurso estavel: a chave de dedup nao pode variar"
        assert evento.detail["notificar"] is False
        assert "webhook" in evento.detail["detalhe"], (
            "o detalhe precisa dizer POR QUE nao notifica -- e o que separa "
            "'decisao' de 'defeito'"
        )

    async def test_a_queda_so_volta_uma_vez(self, agent: Agent) -> None:
        """Um ciclo por 30 s durante uma queda de 1 h sao 120 registros do mesmo
        evento. O registro tem que ser da TRANSICAO."""
        agent._n8n_era_up = True
        agent.health.mark_n8n(False)

        assert len(agent._detecta_queda_do_n8n()) == 1
        for _ in range(10):  # continua fora por mais 10 ciclos
            agent.health.mark_n8n(False)
            assert agent._detecta_queda_do_n8n() == [], (
                "a queda foi registrada de novo -- uma queda longa vira dezenas "
                "de registros identicos, que e ruido sem informacao nova"
            )

    async def test_recuperacao_nao_gera_evento(self, agent: Agent) -> None:
        """`source_down` descreve a QUEDA. "Voltou a responder" nao e um evento
        novo -- e o fim do mesmo, e ja fica no log."""
        agent._n8n_era_up = False
        agent.health.mark_n8n(True)

        assert agent._detecta_queda_do_n8n() == []

    async def test_o_otimo_nunca_vira_queda(self, agent: Agent) -> None:
        """O estado inicial e `None` (ainda nao se sabe). Se `None` contasse
        como queda, o boot registraria uma queda que nao aconteceu."""
        assert agent._n8n_era_up is None
        agent.health.mark_n8n(True)
        assert agent._detecta_queda_do_n8n() == []

    async def test_reinicio_durante_a_queda_nao_reabre_o_evento(self, agent: Agent) -> None:
        """Ate um n8n que sobe e desce entre dois ciclos nao ser visto, o que
        importa e que nao se invente um evento novo a cada leitura."""
        agent._n8n_era_up = False
        for _ in range(3):
            agent.health.mark_n8n(False)
            assert agent._detecta_queda_do_n8n() == []


class TestIntegracaoComAFila:
    async def test_o_evento_e_gravado_mas_nao_entra_na_fila(self, agent: Agent) -> None:
        """O caminho de `notify: false`: gravado em `event`, logado, sem fila.

        E o que `EventPipeline.enqueue:53-56` faz. Nao ha despachante para
        consultar -- e nao deveria haver, ja que nao ha canal de saida.
        """
        agent._n8n_era_up = True
        agent.health.mark_n8n(False)
        eventos = agent._detecta_queda_do_n8n()

        await agent.process_events(eventos)

        assert len(agent.repo.events) == 1, "o rastro tem que ficar gravado"
        assert agent.repo.events[0].event_type == "source_down"
        assert agent.pipeline.queue.items == [], (
            "source_down nao deve virar item de fila: nao ha como despachar"
        )


class TestLigadoNoCiclo:
    async def test_o_ciclo_de_coleta_chama_a_deteccao(self) -> None:
        """Metodo novo sem quem o chama e o mesmo buraco de `global_fallback`.

        Varre `agent.py` por `ast`: se `collect_execution_errors` deixar de
        chamar `_detecta_queda_do_n8n`, a deteccao volta a ser decoracao.
        """
        import ast
        from pathlib import Path

        raiz = Path(__file__).resolve().parents[2] / "src" / "agent_nio_monitor" / "agent.py"
        arvore = ast.parse(raiz.read_text(encoding="utf-8"))

        chamadas_dentro_do_ciclo: set[str] = set()
        for funcao in ast.walk(arvore):
            if not isinstance(funcao, ast.AsyncFunctionDef) or funcao.name != "collect_execution_errors":
                continue
            for no in ast.walk(funcao):
                if isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute):
                    chamadas_dentro_do_ciclo.add(no.func.attr)

        assert "_detecta_queda_do_n8n" in chamadas_dentro_do_ciclo, (
            "a deteccao de queda saiu do ciclo de coleta -- `source_down` volta "
            "a ser regra sem emissor"
        )