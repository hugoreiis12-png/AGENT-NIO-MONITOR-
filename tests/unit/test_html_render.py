"""E-mail HTML com a arte JFC: um template por faixa de severidade."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agent_nio_monitor.config.loader import load
from agent_nio_monitor.config.schema import AppConfig
from agent_nio_monitor.notify.compose import compor
from agent_nio_monitor.notify.html_render import render_html, tema_da_severidade
from agent_nio_monitor.notify.links import link_do_evento
from agent_nio_monitor.store.repository import EventRecord

ANALISE = {
    "resumo": "O container worker parou.",
    "causa_provavel": "Conexao recusada em 10.0.0.5:5432.",
    "como_resolver": ["Verificar o banco.", "Reiniciar o container."],
}


@pytest.fixture
def cfg(monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    monkeypatch.setenv("N8N_API_KEY", "k")
    return load()


def _evento(severidade: str = "critical", **detail) -> EventRecord:
    return EventRecord(
        id=uuid4(), ts=datetime(2026, 10, 13, 19, 0, tzinfo=UTC), source="portainer",
        event_type="container_stopped", severity=severidade, title="Container parado: worker",
        resource_id="abc", detail=detail,
    )


def _html(cfg: AppConfig, severidade: str = "critical", analise: dict | None = ANALISE, **contexto) -> str:
    evento = _evento(severidade, **{k: v for k, v in contexto.items() if k in ("workflow_id", "endpoint_id", "stack_id", "stack", "recurso")})
    return render_html(
        evento=evento, contexto=contexto, analise=analise, abertura="worker parou.",
        acao_padrao="Ver os logs.", assunto="[X] teste", cfg=cfg,
    )


class TestTemaPorSeveridade:
    @pytest.mark.parametrize(
        ("severidade", "hero", "rotulo", "titulo"),
        [
            ("critical", "hero_critico.jpg", "CRÍTICO", "Hora de agir!"),
            ("high", "hero_critico.jpg", "ALTO", "Hora de agir!"),
            ("medium", "hero_medio.jpg", "MÉDIO", "Vamos com calma!"),
            ("low", "hero_baixo.jpg", "BAIXO", "Tudo certo!"),
            ("info", "hero_baixo.jpg", "INFO", "Tudo certo!"),
        ],
    )
    def test_cada_severidade_usa_a_arte_e_o_rotulo_certos(
        self, cfg: AppConfig, severidade: str, hero: str, rotulo: str, titulo: str
    ) -> None:
        html = _html(cfg, severidade)
        assert f"{cfg.mail.assets_base_url}/{hero}" in html
        assert rotulo in html
        assert titulo in html

    def test_cores_dos_tres_templates_sao_distintas(self) -> None:
        cores = {tema_da_severidade(s).cor for s in ("critical", "medium", "low")}
        assert cores == {"#C00917", "#F47F12", "#037328"}

    def test_severidade_desconhecida_cai_no_amarelo(self) -> None:
        assert tema_da_severidade("estranha").hero == "hero_medio.jpg"

    def test_verde_usa_o_selo_de_confirmacao(self, cfg: AppConfig) -> None:
        assert "&#10003;" in _html(cfg, "low") or "✓" in _html(cfg, "low")


class TestConteudoDoCartao:
    def test_secoes_na_ordem_de_leitura(self, cfg: AppConfig) -> None:
        html = _html(cfg, detalhe="Estado: exited", autor="carlos", stack="comprove", workflow_id="wf1")
        ordem = ["O que aconteceu", "Causa provável", "Como resolver", "Detalhes", "Quem mexeu"]
        posicoes = [html.index(t) for t in ordem]
        assert posicoes == sorted(posicoes)

    def test_passos_numerados_e_dados_do_alerta_aparecem(self, cfg: AppConfig) -> None:
        html = _html(cfg, detalhe="Estado: exited", autor="carlos", stack="comprove")
        assert "Verificar o banco." in html and "Reiniciar o container." in html
        assert "Estado: exited" in html
        assert "carlos" in html and "comprove" in html

    def test_horario_em_brasilia(self, cfg: AppConfig) -> None:
        assert "2026-10-13 16:00:00 (Brasília)" in _html(cfg)

    def test_sem_analise_usa_abertura_e_acao_padrao_sem_secao_de_causa(self, cfg: AppConfig) -> None:
        html = _html(cfg, analise=None)
        assert "worker parou." in html and "Ver os logs." in html
        assert "Causa provável" not in html

    def test_secoes_vazias_nao_imprimem_titulo(self, cfg: AppConfig) -> None:
        html = _html(cfg)
        assert "Quem mexeu" not in html
        assert "Log recente" not in html

    def test_log_recente_aparece_em_bloco_proprio(self, cfg: AppConfig) -> None:
        html = _html(cfg, logs="Error: connect ECONNREFUSED")
        assert "Log recente" in html and "ECONNREFUSED" in html

    def test_rodape_tem_o_email_de_suporte_da_arte(self, cfg: AppConfig) -> None:
        assert f"mailto:{cfg.mail.support_email}" in _html(cfg)


class TestSegurancaDoHtml:
    PAYLOAD = "<script>alert(1)</script> & <img src=x onerror=alert(2)>"

    def test_conteudo_do_evento_e_escapado(self, cfg: AppConfig) -> None:
        html = _html(cfg, erro=self.PAYLOAD, detalhe=self.PAYLOAD, logs=self.PAYLOAD, autor=self.PAYLOAD,
                     analise={**ANALISE, "resumo": self.PAYLOAD, "como_resolver": [self.PAYLOAD]})
        assert "<script>" not in html
        assert "<img src=x" not in html
        assert "&lt;script&gt;" in html

    def test_titulo_do_alerta_tambem_e_escapado(self, cfg: AppConfig) -> None:
        evento = _evento()
        evento.title = self.PAYLOAD
        html = render_html(evento=evento, contexto={}, analise=ANALISE, abertura="a",
                           acao_padrao="b", assunto="s", cfg=cfg)
        assert "<script>" not in html

    def test_link_nao_vem_de_conteudo_do_evento(self, cfg: AppConfig) -> None:
        html = _html(cfg, url="https://malicioso.example/x", workflow_id="wf1")
        assert "malicioso.example" not in html


class TestLinkDoBotao:
    def test_workflow_aponta_para_o_n8n_da_config(self, cfg: AppConfig) -> None:
        texto, url = link_do_evento({"workflow_id": "wf1"}, cfg)
        assert texto == "Abrir no n8n"
        assert url == f"{cfg.active.n8n.url.rstrip('/')}/workflow/wf1"

    def test_identificador_e_codificado(self, cfg: AppConfig) -> None:
        _, url = link_do_evento({"workflow_id": "a/b?c"}, cfg)
        assert url.endswith("/workflow/a%2Fb%3Fc")

    def test_container_aponta_para_o_portainer(self, cfg: AppConfig) -> None:
        texto, url = link_do_evento({"recurso": "worker", "endpoint_id": 3}, cfg)
        assert texto == "Abrir no Portainer"
        assert url.endswith("/#!/3/docker/containers/worker")

    def test_stack_aponta_para_a_pagina_da_stack(self, cfg: AppConfig) -> None:
        _, url = link_do_evento({"stack": "comprove", "stack_id": 7, "endpoint_id": 3, "recurso": "comprove"}, cfg)
        assert url.endswith("/#!/3/docker/stacks/comprove?id=7&type=2&regular=true")

    def test_sem_destino_conhecido_nao_ha_botao(self, cfg: AppConfig) -> None:
        assert link_do_evento({"recurso": "x"}, cfg) is None
        assert "<a href=\"http" not in _html(cfg, recurso="x").split("Se tiver")[0].split("Detalhes")[-1]


class _AnalistaFixo:
    def __init__(self, analise) -> None:  # noqa: ANN001
        self.analise = analise

    async def analyze(self, contexto: dict):  # noqa: ANN201
        return self.analise, "llm" if self.analise else "fallback"


class TestCompor:
    async def test_devolve_texto_html_e_status(self, cfg: AppConfig) -> None:
        from agent_nio_monitor.llm.analyst import Analysis

        analise = Analysis(ANALISE["resumo"], ANALISE["causa_provavel"], ANALISE["como_resolver"])
        evento = _evento(template="container_down", recurso="worker")
        msg = await compor(_AnalistaFixo(analise), evento, {"tipo": "container_stopped", "titulo": "t", "severidade": "critical", "recurso": "worker"}, cfg)
        assert msg.status_llm == "llm"
        assert "Causa provavel" in msg.corpo
        assert msg.html.startswith("<!doctype html>")

    async def test_sem_llm_o_alerta_sai_com_texto_e_html(self, cfg: AppConfig) -> None:
        evento = _evento(template="container_down", recurso="worker")
        msg = await compor(_AnalistaFixo(None), evento, {"tipo": "x", "titulo": "t", "severidade": "high"}, cfg)
        assert msg.status_llm == "fallback"
        assert msg.corpo and "Hora de agir!" in msg.html
