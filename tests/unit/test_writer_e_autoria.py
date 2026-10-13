"""Analista do LLM nao pode inventar fato; e-mail mostra quem mexeu."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from agent_nio_monitor.llm.analyst import AlertAnalyst, abertura_fallback
from agent_nio_monitor.llm.client import LLMUnavailable
from agent_nio_monitor.notify.templates import render
from agent_nio_monitor.store.repository import EventRecord

CTX = {"tipo": "container_stopped", "titulo": "Container parado: worker", "severidade": "critical",
       "recurso": "worker", "detalhe": "Estado atual: exited", "stack": "comprove", "autor": "carlos",
       "logs": "connect ECONNREFUSED 10.0.0.5:443"}
OK = {"resumo": "O container worker parou.", "causa_provavel": "Conexao recusada em 10.0.0.5:443.",
      "como_resolver": ["Verificar o servico em 10.0.0.5.", "Reiniciar o container."]}


class _Llm:
    def __init__(self, resposta: dict | Exception) -> None:
        self.resposta, self.system, self.user = resposta, "", ""

    async def chat_json(self, system: str, user: str) -> dict:
        self.system, self.user = system, user
        if isinstance(self.resposta, Exception):
            raise self.resposta
        return self.resposta


class TestAnalista:
    async def test_prompt_leva_os_fatos_reais_incluindo_o_log(self) -> None:
        llm = _Llm(OK)
        await AlertAnalyst(llm).analyze(CTX)
        for fato in ("worker", "Estado atual: exited", "comprove", "carlos", "ECONNREFUSED"):
            assert fato in llm.user

    async def test_analise_valida_e_aceita(self) -> None:
        analise, status = await AlertAnalyst(_Llm(OK)).analyze(CTX)
        assert status == "llm"
        assert analise is not None and analise.como_resolver[0].startswith("Verificar")

    async def test_data_inventada_pelo_llm_cai_no_fallback(self) -> None:
        inventado = {**OK, "resumo": "Em 28/03/2023 o worker parou."}
        analise, status = await AlertAnalyst(_Llm(inventado)).analyze(CTX)
        assert (analise, status) == (None, "fallback")

    async def test_formato_invalido_cai_no_fallback(self) -> None:
        for ruim in ({}, {**OK, "como_resolver": []}, {**OK, "resumo": ""}, {**OK, "como_resolver": "x"}):
            assert await AlertAnalyst(_Llm(ruim)).analyze(CTX) == (None, "fallback")

    async def test_llm_fora_do_ar_nao_levanta(self) -> None:
        assert await AlertAnalyst(_Llm(LLMUnavailable("x"))).analyze(CTX) == (None, "fallback")

    async def test_desligado_nem_chama_o_llm(self) -> None:
        llm = _Llm(OK)
        assert await AlertAnalyst(llm, enabled=False).analyze(CTX) == (None, "desligado")
        assert llm.user == ""

    def test_fallback_deterministico_usa_o_titulo(self) -> None:
        assert abertura_fallback(CTX) == "Container parado: worker."


def _evento(**kw) -> EventRecord:
    base = {"id": uuid4(), "ts": datetime(2026, 10, 13, 12, 0, tzinfo=UTC), "source": "portainer",
            "event_type": "container_stopped", "severity": "critical", "title": "Container parado: worker",
            "resource_id": "abc"}
    return EventRecord(**{**base, **kw})


class TestEmailMostraQuemMexeu:
    def test_container_mostra_stack_dono_e_ultimo_autor(self) -> None:
        _, corpo = render("generic", event=_evento(), contexto={
            "stack": "comprove", "responsavel_stack": "admin", "autor": "carlos"})
        assert "Stack: comprove" in corpo
        assert "Responsavel pela stack: admin" in corpo
        assert "Ultima alteracao por: carlos" in corpo

    def test_sem_autoria_nao_imprime_bloco_vazio(self) -> None:
        _, corpo = render("generic", event=_evento(), contexto={})
        assert "Quem mexeu" not in corpo

    def test_workflow_alterado_nao_repete_o_autor(self) -> None:
        _, corpo = render("workflow_changed", event=_evento(event_type="workflow_changed"),
                          contexto={"autor": "Ana <ana@x.com>", "workflow_nome": "wf"})
        assert corpo.count("Ana <ana@x.com>") == 1


class TestEmailComAnalise:
    def _corpo(self, **extra) -> str:
        return render("container_down", event=_evento(),
                      contexto={**CTX, "analise": OK, **extra})[1]

    def test_secoes_na_ordem_de_leitura(self) -> None:
        corpo = self._corpo()
        ordem = ["O que aconteceu", "Causa provavel", "Como resolver", "Detalhes", "Quem mexeu"]
        posicoes = [corpo.index(t) for t in ordem]
        assert posicoes == sorted(posicoes)

    def test_passos_numerados_e_log_nos_detalhes(self) -> None:
        corpo = self._corpo()
        assert "1. Verificar o servico em 10.0.0.5." in corpo
        assert "2. Reiniciar o container." in corpo
        assert "Log recente: connect ECONNREFUSED" in corpo

    def test_stack_e_autor_aparecem_uma_vez_so(self) -> None:
        corpo = self._corpo()
        assert corpo.count("Stack: comprove") == 1
        assert corpo.count("Ultima alteracao por: carlos") == 1

    def test_sem_passos_usa_a_acao_padrao_do_template(self) -> None:
        corpo = self._corpo(analise={**OK, "como_resolver": []})
        assert "1. Verificar os logs do container" in corpo


class TestGuardDeNumero:
    async def test_digito_dentro_de_palavra_nao_conta_como_numero(self) -> None:
        com_n8n = {**OK, "resumo": "O n8n nao conseguiu conectar em 10.0.0.5:443."}
        analise, status = await AlertAnalyst(_Llm(com_n8n)).analyze(CTX)
        assert status == "llm" and analise is not None


def test_fato_repetido_em_dois_campos_aparece_uma_vez() -> None:
    from agent_nio_monitor.llm.facts import fatos

    assert fatos({"workflow_nome": "worker", "recurso": "worker"}).count("Recurso: worker") == 1
