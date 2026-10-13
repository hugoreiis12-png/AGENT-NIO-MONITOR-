"""Redator do LLM nao pode inventar fato; e-mail mostra quem mexeu."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from agent_nio_monitor.llm.writer import AlertWriter, LLMUnavailable
from agent_nio_monitor.notify.templates import render
from agent_nio_monitor.store.repository import EventRecord

CTX = {"tipo": "container_stopped", "titulo": "Container parado: worker", "severidade": "critical",
       "recurso": "worker", "detalhe": "Estado atual: exited", "stack": "comprove", "autor": "carlos"}


class _Llm:
    def __init__(self, resposta: str | Exception) -> None:
        self.resposta, self.prompt = resposta, ""

    async def generate(self, prompt: str) -> str:
        self.prompt = prompt
        if isinstance(self.resposta, Exception):
            raise self.resposta
        return self.resposta


class TestRedator:
    async def test_prompt_leva_os_fatos_reais_do_evento(self) -> None:
        llm = _Llm("O container worker parou.")
        await AlertWriter(llm).write(CTX)
        assert "worker" in llm.prompt
        assert "Estado atual: exited" in llm.prompt
        assert "comprove" in llm.prompt
        assert "carlos" in llm.prompt

    async def test_data_inventada_pelo_llm_cai_no_fallback(self) -> None:
        llm = _Llm("Em 28/03/2023 as 14:30, o worker parou.")
        texto, status = await AlertWriter(llm).write(CTX)
        assert status == "fallback"
        assert "2023" not in texto

    async def test_numero_presente_nos_fatos_e_aceito(self) -> None:
        llm = _Llm("O worker saiu com codigo 1.")
        _, status = await AlertWriter(llm).write({**CTX, "detalhe": "Exited (1)"})
        assert status == "llm"

    async def test_llm_fora_do_ar_usa_fallback_sem_levantar(self) -> None:
        texto, status = await AlertWriter(_Llm(LLMUnavailable("x"))).write(CTX)
        assert status == "fallback"
        assert texto == "Container parado: worker."


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
