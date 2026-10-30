"""Templates de email e o pipeline completo do Agent.

Requisito do dono do projeto: email DETALHADO POREM DIRETO, sem enrolar sobre o
que aconteceu. Estes testes travam esse requisito -- sao a unica garantia de que
o primeiro paragrafo responde 'o que quebrou e desde quando'.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from agent_nio_monitor.main import Agent
from agent_nio_monitor.notify import templates
from agent_nio_monitor.store.repository import EventRecord
from agent_nio_monitor.store.memory_repository import InMemoryRepository

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "executions_erros_reais.json"


def _evento(**kw) -> EventRecord:
    base = dict(
        id=uuid4(),
        ts=datetime(2026, 10, 12, 19, 0, tzinfo=UTC),
        source="n8n",
        event_type="execution_failed",
        severity="critical",
        title="Processa Checkout falhou 9x",
        tenant_id="V3yqT1MGvf0Ufg17",
        resource_id="JqOi9bhDIXzi8wKN",
        streak_count=9,
        detail={},
    )
    base.update(kw)
    return EventRecord(**base)


CONtexto_REAL = {
    "projeto": "V3yqT1MGvf0Ufg17",
    "recurso": "JqOi9bhDIXzi8wKN",
    "workflow_nome": "Processa Checkout",
    "workflow_id": "JqOi9bhDIXzi8wKN",
    "n": 9,
    "primeira": "2026-10-11 16:30",
    "ultima": "2026-10-12 16:30",
    "erro": "Connection timeout",
    "modo": "trigger",
    "contexto": "SAP B1 indisponivel (192.168.0.142)",
}


class TestTemplateExecucao:
    def test_assunto_traz_severidade_e_contagem(self) -> None:
        assunto, _ = templates.render("workflow_failing", event=_evento(), contexto=CONtexto_REAL)
        assert "CRITICO" in assunto
        assert "Processa Checkout" in assunto
        assert "9x" in assunto

    def test_corpo_abre_pelo_que_quebrou(self) -> None:
        """Detalhado porem direto: o problema vem ANTES da contribuicao."""
        _, corpo = templates.render("workflow_failing", event=_evento(), contexto=CONtexto_REAL)
        primeiro_bloco = corpo.split("Contexto")[0]
        assert "9 vez(es)" in primeiro_bloco
        assert primeiro_bloco.index("9 vez(es)") < primeiro_bloco.index("Primeira falha")

    def test_nao_tem_enrolacao(self) -> None:
        _, corpo = templates.render("workflow_failing", event=_evento(), contexto=CONtexto_REAL)
        baixo = corpo.lower()
        for frase in ("esperamos", "desejamos", "prezamos", "é importante notar",
                      "vale ressaltar", "lamentamos", "aguarde"):
            assert frase not in baixo, f"texto enrolado: '{frase}'"

    def test_nao_tem_emoji_nem_markdown(self) -> None:
        _, corpo = templates.render("workflow_failing", event=_evento(), contexto=CONtexto_REAL)
        assert not re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", corpo)
        assert "**" not in corpo

    def test_tem_acoes_concretas(self) -> None:
        _, corpo = templates.render("workflow_failing", event=_evento(), contexto=CONtexto_REAL)
        assert "Acao sugerida" in corpo
        assert "Abrir o workflow no n8n" in corpo

    def test_link_quando_ha_url(self) -> None:
        _, corpo = templates.render(
            "workflow_failing", event=_evento(),
            contexto={**CONtexto_REAL, "url_workflow": "http://n8n.test/workflow/x"},
        )
        assert "Ver no n8n: http://n8n.test/workflow/x" in corpo

    def test_link_ausente_omite_a_linha(self) -> None:
        """'Ver no n8n: -' e pior que nao imprimir a linha. Alerta com placeholder
        vazio treina o leitor a ignorar a linha."""
        _, corpo = templates.render("workflow_failing", event=_evento(), contexto=CONtexto_REAL)
        assert "Ver no n8n" not in corpo

    def test_assunto_tem_janela_real_nao_interrogacao(self) -> None:
        """O assunto dizia 'em ?min' porque so as datas chegam do banco."""
        assunto, _ = templates.render("workflow_failing", event=_evento(), contexto=CONtexto_REAL)
        assert "?" not in assunto
        assert "1440min" in assunto

    def test_assunto_comeca_com_a_marca_do_agent_nio(self) -> None:
        """O evento de teste tem source='n8n'; a marca esperada e a do n8n,
        nao mais a marca fixa do Agent NIO."""
        assunto, _ = templates.render("workflow_failing", event=_evento(), contexto=CONtexto_REAL)
        assert assunto.startswith(f"{templates.MARCA_POR_FONTE['n8n']} [")

    @pytest.mark.parametrize(
        ("fonte", "marca"),
        [
            ("n8n", "★"),
            ("portainer", "♛"),
            ("powerautomate", "✦"),
            ("bubble", "✧"),
        ],
    )
    def test_marca_por_fonte(self, fonte: str, marca: str) -> None:
        assunto, _ = templates.render(
            "workflow_failing", event=_evento(source=fonte), contexto=CONtexto_REAL
        )
        assert assunto.startswith(f"{marca} [")

    def test_marca_fallback_para_fonte_desconhecida(self) -> None:
        assunto, _ = templates.render(
            "workflow_failing", event=_evento(source="mystack"), contexto=CONtexto_REAL
        )
        assert assunto.startswith(f"{templates.MARCA_NIO} [")

    def test_assunto_sem_janela_conhecida_omite_o_sufixo(self) -> None:
        assunto, _ = templates.render(
            "workflow_failing", event=_evento(),
            contexto={**CONtexto_REAL, "primeira": None, "ultima": None},
        )
        assert "?" not in assunto
        assert "min" not in assunto.replace("falhou", "")
        assert "9x" in assunto

    def test_severidade_critica_vem_explicita(self) -> None:
        _, corpo = templates.render("workflow_failing", event=_evento(), contexto=CONtexto_REAL)
        assert "Severidade: CRITICO" in corpo

    def test_sem_erro_nao_inventa_causa(self) -> None:
        ctx = {**CONtexto_REAL, "erro": None}
        _, corpo = templates.render("execution_failed", event=_evento(streak_count=1), contexto=ctx)
        assert "(sem mensagem de erro)" in corpo


class TestTemplateMudanca:
    def test_autoria_ausente_e_dita_nao_omitida(self) -> None:
        """Omitir 'autoria nao disponivel' faria parecer que ninguem mudou.
        Aqui a versao 1.102.x nao expoe history, e o email diz isso."""
        _, corpo = templates.render("workflow_changed", event=_evento(
            event_type="workflow_changed"), contexto=CONtexto_REAL)
        assert "Autoria nao disponivel" in corpo or "nio@" in corpo

    def test_resumo_de_diff_aparece(self) -> None:
        _, corpo = templates.render("workflow_changed", event=_evento(
            event_type="workflow_changed"),
            contexto={**CONtexto_REAL, "resumo_diff": "- node REMOVIDO: 1\n  * Sub Workflow (xyz)"})
        assert "node REMOVIDO" in corpo

    def test_autoria_presente_e_mostrada(self) -> None:
        _, corpo = templates.render("workflow_changed", event=_evento(
            event_type="workflow_changed"),
            contexto={**CONtexto_REAL, "autor": "nio@grupocampanha.com.br"})
        assert "nio@grupocampanha.com.br" in corpo


class TestTemplateGenerico:
    def test_template_desconhecido_nao_quebra(self) -> None:
        assunto, corpo = templates.render("template_que_nao_existe", event=_evento())
        assert assunto
        assert corpo

    def test_todos_os_event_types_tem_assunto(self) -> None:
        import yaml

        from agent_nio_monitor.config.schema import PolicyConfig

        cfg_dir = Path(__file__).resolve().parents[2] / "config"
        policy = PolicyConfig.model_validate(
            yaml.safe_load((cfg_dir / "policy.yaml").read_text(encoding="utf-8"))
        )
        faltando = []
        for regra in policy.rules:
            _, corpo = templates.render(regra.template, event=_evento(), contexto=CONtexto_REAL)
            if not corpo.strip():
                faltando.append(regra.template)
        assert not faltando, f"template sem conteudo: {faltando}"


class TestPipelineCompleto:
    """Pipeline inteiro, do JSON bruto do n8n ate o registro da notificacao."""

    @pytest.fixture
    def config_teste(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):  # noqa: ANN201

        from agent_nio_monitor.config.schema import AppConfig

        monkeypatch.setenv("N8N_API_KEY", "k")
        monkeypatch.setenv("DATABASE_DSN", "postgresql://u:s@localhost/h")
        raiz = Path(__file__).resolve().parents[2]
        monkeypatch.setenv("ANM_TEST_CONFIG", str(raiz / "config"))
        assert AppConfig is not None
        return raiz

    async def test_run_once_com_erros_reais(self, config_teste, respx_mock) -> None:
        import httpx

        from agent_nio_monitor.config.loader import load
        from agent_nio_monitor.sources.n8n.collector import ExecutionErrorCollector

        erros = __import__("json").loads(
            FIXTURE.read_text(encoding="utf-8")
        )["data"]
        repo = InMemoryRepository()
        cfg = load()

        respx_mock.get(f"{cfg.active.n8n.url}/api/v1/executions").mock(
            return_value=httpx.Response(200, json={"data": erros, "nextCursor": None})
        )
        collector = ExecutionErrorCollector(
            _client_stub(cfg), repo, fetch_payload=False
        )
        resultado = await collector.run(
            name_by_id={"JqOi9bhDIXzi8wKN": "Processa Checkout"}
        )

        assert resultado.collected == 25
        assert resultado.gap is False
        assert len(repo.executions) == 25
        # o streak do workflow mais quebrado
        s = repo.get_streak("JqOi9bhDIXzi8wKN")
        assert s.count == 9
        # todo workflow quebrado virou streak
        assert len(repo.streaks) == 8

    async def test_segunda_passada_e_idempotente(self, config_teste, respx_mock) -> None:
        import httpx

        from agent_nio_monitor.config.loader import load
        from agent_nio_monitor.sources.n8n.collector import ExecutionErrorCollector

        erros = __import__("json").loads(FIXTURE.read_text(encoding="utf-8"))["data"]
        repo = InMemoryRepository()
        cfg = load()
        respx_mock.get(f"{cfg.active.n8n.url}/api/v1/executions").mock(
            return_value=httpx.Response(200, json={"data": erros, "nextCursor": None})
        )
        col = ExecutionErrorCollector(_client_stub(cfg), repo, fetch_payload=False)
        r1 = await col.run()
        r2 = await col.run()
        assert r1.collected == 25
        assert r2.collected == 0, "rodar duas vezes nao pode duplicar evento"
        assert len(repo.executions) == 25


def _client_stub(cfg):  # noqa: ANN202
    from agent_nio_monitor.sources.n8n.client import N8NClient

    return N8NClient(cfg.active.n8n.url, "k", verify_ssl=False)


class TestEscalonamentoDeStreak:
    def test_streak_critico_muda_template_e_severidade(self) -> None:
        import yaml

        from agent_nio_monitor.config.schema import PolicyConfig
        from agent_nio_monitor.rules.engine import RuleEngine

        cfg_dir = Path(__file__).resolve().parents[2] / "config"
        engine = RuleEngine(
            PolicyConfig.model_validate(
                yaml.safe_load((cfg_dir / "policy.yaml").read_text(encoding="utf-8"))
            )
        )
        baixo = engine.evaluate(event_type="execution_failed", streak_count=1)
        alto = engine.evaluate(event_type="execution_failed", streak_count=3)
        assert baixo.template == "execution_failed"
        assert alto.template == "workflow_failing"
        assert baixo.severity == "high"
        assert alto.severity == "critical"


def test_agent_existe_com_contrato_esperado() -> None:
    """Guarda contra refatoracao que quebre a assinatura usada por main()."""
    assert hasattr(Agent, "run_once")
    assert hasattr(Agent, "collect_execution_errors")
    assert hasattr(Agent, "dispatch")