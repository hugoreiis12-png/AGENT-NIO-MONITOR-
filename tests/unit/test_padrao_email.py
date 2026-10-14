"""Padrao do e-mail de alerta, aprovado pelo dono (caso real: hfbrasil-preco pausado).

Passa pelo caminho de producao (`Agent.dispatch` -> `sender` -> `compor` -> HTML). Se uma
secao sumir, mudar de nome ou de ordem, este teste quebra antes do e-mail chegar diferente.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape

from agent_nio_monitor.agent import Agent
from agent_nio_monitor.config.loader import load
from agent_nio_monitor.llm.analyst import Analysis
from agent_nio_monitor.notify.mailer import DispatchResult
from agent_nio_monitor.store.memory_repository import InMemoryRepository
from agent_nio_monitor.store.repository import EventRecord, new_event_id

ORDEM_DAS_SECOES = [
    "O que aconteceu",
    "Causa provável",
    "Como resolver",
    "Detalhes",
    "Recurso",
    "Detalhe",
    "Quando",
    "Log recente",
    "Quem mexeu",
    "Stack",
    "Responsável pela stack",
    "Última alteração por",
    "Abrir no Portainer",
    "Se tiver dúvidas, entre em contato com nossa equipe:",
    "help@jfcagro.com.br",
    "Enviado automaticamente pelo Hermes Monitor",
]


class _Analista:
    async def analyze(self, contexto: dict) -> tuple[Analysis, str]:
        return Analysis(
            resumo="O container hfbrasil-preco da stack automacao_hf_tarefas está parado no estado paused.",
            causa_provavel="O container foi pausado manualmente pelo usuário hugo.",
            como_resolver=[
                "Verifique com o usuário hugo se a pausa foi intencional.",
                "Se a pausa não for intencional, execute docker start hfbrasil-preco.",
                "Confirme o estado do container após a ação.",
            ],
        ), "llm"


class _Captura:
    async def send(self, **kwargs: object) -> DispatchResult:
        self.enviado = kwargs
        return DispatchResult(status="sent", channel="teste")


def _container_pausado() -> EventRecord:
    return EventRecord(
        id=new_event_id(), ts=datetime(2026, 10, 13, 21, 6, 23, tzinfo=UTC), source="portainer",
        event_type="container_stopped", severity="critical", title="Container parado: hfbrasil-preco",
        resource_id="abc",
        detail={
            "recurso": "hfbrasil-preco", "workflow_nome": "hfbrasil-preco",
            "detalhe": "Estado atual: paused (Up 5 days (Paused))",
            "stack": "automacao_hf_tarefas", "autor": "hugo", "responsavel_stack": "hugo",
            "endpoint_id": 3, "logs": "[2026-10-12 07:01:28] INFO hfbrasil | Pipeline finalizada",
            "notificar": True, "template": "container_down",
        },
    )


async def _linhas_do_email() -> list[str]:
    agente = Agent(load(), InMemoryRepository())
    agente.analyst, agente.notifier = _Analista(), _Captura()
    try:
        await agente.dispatch(_container_pausado())
    finally:
        await agente.llm.aclose()
        await agente.n8n.aclose()
    texto = unescape(re.sub(r"<[^>]+>", "\n", agente.notifier.enviado["html"]))
    return [linha.strip() for linha in texto.splitlines() if linha.strip()]


async def test_secoes_na_ordem_aprovada() -> None:
    linhas = await _linhas_do_email()
    posicoes = [linhas.index(secao) for secao in ORDEM_DAS_SECOES if secao in linhas]
    faltando = [secao for secao in ORDEM_DAS_SECOES if secao not in linhas]
    assert not faltando, f"secao sumiu do e-mail: {faltando}"
    assert posicoes == sorted(posicoes), "secoes fora da ordem aprovada"


async def test_conteudo_de_cada_secao() -> None:
    linhas = await _linhas_do_email()

    def depois(rotulo: str) -> str:
        return linhas[linhas.index(rotulo) + 1]

    assert depois("O que aconteceu").startswith("O container hfbrasil-preco")
    assert depois("Recurso") == "hfbrasil-preco"
    assert depois("Detalhe") == "Estado atual: paused (Up 5 days (Paused))"
    assert depois("Quando") == "2026-10-13 18:06:23 (Brasília)", "horario tem que sair em Brasilia"
    assert depois("Log recente").startswith("[2026-10-12 07:01:28]")
    assert depois("Stack") == "automacao_hf_tarefas"
    assert depois("Responsável pela stack") == "hugo"
    assert depois("Última alteração por") == "hugo"
    passos = linhas[linhas.index("Como resolver") + 1:linhas.index("Detalhes")]
    assert passos[0::2] == ["1.", "2.", "3."], "passos numerados"
