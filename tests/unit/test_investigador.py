"""Qwen investigador (ADR D42): ferramentas de leitura, evidencia conferida e confianca honesta.

O Qwen e roteirizado aqui: o formato das respostas (`tool_calls` estruturado) e o que o vLLM
real devolveu em 19/10/2026 com o parser Hermes ativo.
"""

from __future__ import annotations

import asyncio
import json

from agent_nio_monitor.investigation_tools import InvestigationTools
from agent_nio_monitor.llm.analyst import AlertAnalyst
from agent_nio_monitor.llm.client import LLMUnavailable
from agent_nio_monitor.llm.investigator import Investigator, evidencias_conferidas
from agent_nio_monitor.notify.corpo_analise import corpo_com_analise

LOG_OOM = "2026-10-19T12:00:01Z FATAL: JavaScript heap out of memory"
CONTEXTO = {"tipo": "container_stopped", "titulo": "Container parado: api", "severidade": "critical",
            "fonte": "portainer", "recurso": "api", "detalhe": "Estado atual: exited"}


def _chamada(nome: str, **args) -> dict:
    return {"id": f"c-{nome}", "type": "function", "function": {"name": nome, "arguments": json.dumps(args)}}


class QwenRoteirizado:
    def __init__(self, rodadas: list[dict], final: dict | Exception) -> None:
        self.rodadas, self.final, self.mensagens = list(rodadas), final, []

    async def chat_tools(self, mensagens: list, tools: list) -> dict:
        self.ferramentas_oferecidas = [t["function"]["name"] for t in tools]
        return self.rodadas.pop(0) if self.rodadas else {"content": "pronto", "tool_calls": None}

    async def chat_json_messages(self, mensagens: list) -> dict:
        self.mensagens = mensagens
        if isinstance(self.final, Exception):
            raise self.final
        return self.final

    async def chat_json(self, system: str, user: str) -> dict:
        return {"resumo": "api parou.", "causa_provavel": "Nao determinada.", "como_resolver": ["Ver log", "Subir"]}


class FerramentasFalsas:
    def __init__(self, saidas: dict[str, str]) -> None:
        self.saidas, self.chamadas = saidas, []

    def esquemas(self, fonte: str) -> list[dict]:
        return [{"type": "function", "function": {"name": n}} for n in self.saidas] if fonte == "portainer" else []

    async def executar(self, nome: str, argumentos: str | dict) -> str:
        self.chamadas.append((nome, json.loads(argumentos)))
        return self.saidas[nome]


def _final(**extra) -> dict:
    return {"resumo": "api parou por falta de memoria.", "causa_provavel": "O processo Node estourou o heap.",
            "como_resolver": ["Aumentar o limite de memoria", "Recriar o container"], **extra}


class TestInvestigacao:
    async def test_chama_a_ferramenta_e_conclui_com_evidencia_conferida(self) -> None:
        qwen = QwenRoteirizado(
            [{"content": None, "tool_calls": [_chamada("log_do_container", container="api", filtro="memory")]}],
            _final(evidencias=["FATAL: JavaScript heap out of memory", "OOM confirmado pelo kernel as 11h"],
                   confianca="alta"),
        )
        ferramentas = FerramentasFalsas({"log_do_container": LOG_OOM})
        analise = await Investigator(qwen, ferramentas).investigar(CONTEXTO, "portainer")
        assert ferramentas.chamadas == [("log_do_container", {"container": "api", "filtro": "memory"})]
        assert analise.evidencias == ["FATAL: JavaScript heap out of memory"], "evidencia inventada e descartada"
        assert analise.confianca == "alta"
        assert any(m.get("role") == "tool" and LOG_OOM in m["content"] for m in qwen.mensagens)

    async def test_sem_evidencia_conferida_a_confianca_cai_para_baixa(self) -> None:
        qwen = QwenRoteirizado([], _final(evidencias=["algo que ninguem leu em lugar nenhum"], confianca="alta"))
        analise = await Investigator(qwen, FerramentasFalsas({"log_do_container": LOG_OOM})).investigar(
            CONTEXTO, "portainer")
        assert analise.evidencias == [] and analise.confianca == "baixa"

    async def test_numero_inventado_rejeita_a_investigacao(self) -> None:
        qwen = QwenRoteirizado([], {**_final(), "causa_provavel": "Estourou 4096 MB de heap."})
        assert await Investigator(qwen, FerramentasFalsas({"x": LOG_OOM})).investigar(CONTEXTO, "portainer") is None

    async def test_prazo_estourado_devolve_none(self) -> None:
        class Lento(QwenRoteirizado):
            async def chat_tools(self, mensagens: list, tools: list) -> dict:
                await asyncio.sleep(5)
                return {}

        investigador = Investigator(Lento([], _final()), FerramentasFalsas({"x": LOG_OOM}), prazo=0.05)
        assert await investigador.investigar(CONTEXTO, "portainer") is None

    async def test_fonte_sem_ferramenta_nao_investiga(self) -> None:
        assert await Investigator(QwenRoteirizado([], _final()), FerramentasFalsas({"x": "y"})).investigar(
            CONTEXTO, "n8n") is None

    def test_evidencia_ignora_espaco_e_caixa_mas_exige_o_texto(self) -> None:
        base = "linha 1\nERRO   Conexao recusada pelo SAP\nlinha 3"
        assert evidencias_conferidas(["erro conexao recusada pelo sap"], base) == ["erro conexao recusada pelo sap"]
        assert evidencias_conferidas(["erro conexao recusada pelo banco"], base) == []


class TestAnalista:
    async def test_investigacao_vem_primeiro(self) -> None:
        qwen = QwenRoteirizado([], _final(evidencias=["FATAL: JavaScript heap out of memory"], confianca="media"))
        investigador = Investigator(qwen, FerramentasFalsas({"log_do_container": LOG_OOM}))
        qwen.rodadas = [{"tool_calls": [_chamada("log_do_container", container="api")]}]
        analise, status = await AlertAnalyst(qwen, investigador=investigador).analyze(CONTEXTO)
        assert status == "investigado" and analise.confianca == "media"

    async def test_investigacao_que_falha_cai_na_analise_simples(self) -> None:
        qwen = QwenRoteirizado([], LLMUnavailable("vLLM fora"))
        investigador = Investigator(qwen, FerramentasFalsas({"log_do_container": LOG_OOM}))
        analise, status = await AlertAnalyst(qwen, investigador=investigador).analyze(CONTEXTO)
        assert status == "llm" and analise.confianca is None


class TestFerramentasReais:
    def test_fonte_desligada_nao_oferece_a_ferramenta(self) -> None:
        ferramentas = InvestigationTools(n8n=object(), portainer=None, dataverse=None)
        assert ferramentas.esquemas("portainer") == []
        assert [e["function"]["name"] for e in ferramentas.esquemas("n8n")] == ["execucao_n8n", "ultimas_execucoes_n8n"]

    async def test_inspect_nao_vaza_env_do_container(self) -> None:
        class Portainer:
            async def list_endpoints(self) -> list:
                return [{"Id": 3}]

            async def list_containers(self, _e: int) -> list:
                return [{"Id": "abc", "Names": ["/api"]}]

            async def inspect_container(self, _e: int, _c: str) -> dict:
                return {"State": {"Status": "exited", "ExitCode": 137, "OOMKilled": True}, "RestartCount": 4,
                        "Config": {"Image": "api:1", "Env": ["DB_PASSWORD=hunter2"]}}

        saida = await InvestigationTools(n8n=object(), portainer=Portainer(), dataverse=None).executar(
            "inspecionar_container", '{"container": "api"}')
        assert '"oom_killed": true' in saida and '"exit_code": 137' in saida
        assert "hunter2" not in saida and "DB_PASSWORD" not in saida

    async def test_erro_de_ferramenta_vira_texto(self) -> None:
        ferramentas = InvestigationTools(n8n=object(), portainer=None, dataverse=None)
        assert (await ferramentas.executar("nao_existe", "{}")).startswith("erro: ferramenta desconhecida")
        assert (await ferramentas.executar("execucao_n8n", '{"execucao_id": "1"}')).startswith("erro: ")


def test_email_em_texto_mostra_confianca_e_evidencias() -> None:
    analise = {"resumo": "api parou.", "causa_provavel": "Heap estourou.", "como_resolver": ["Subir limite"],
               "evidencias": ["FATAL: JavaScript heap out of memory"], "confianca": "alta"}
    corpo = corpo_com_analise({"analise": analise}, "acao")
    assert "Causa apurada (confianca alta)" in corpo
    assert "- FATAL: JavaScript heap out of memory" in corpo
