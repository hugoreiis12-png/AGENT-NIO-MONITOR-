"""Contrato do cliente OpenAI-compatível (Qwen no vLLM)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from agent_nio_monitor.llm.client import LlmClient, LLMUnavailable

BASE = "http://llm.test/v1"


def _resposta(conteudo: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": conteudo}}]})


@respx.mock
async def test_pede_json_sem_thinking_e_devolve_o_objeto() -> None:
    rota = respx.post(f"{BASE}/chat/completions").mock(return_value=_resposta('{"resumo": "ok"}'))
    dados = await LlmClient(BASE, "qwen").chat_json("sistema", "usuario")
    corpo = json.loads(rota.calls[0].request.content)
    assert dados == {"resumo": "ok"}
    assert corpo["chat_template_kwargs"] == {"enable_thinking": False}
    assert corpo["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in corpo["messages"]] == ["system", "user"]


@respx.mock
async def test_ignora_bloco_think_antes_do_json() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=_resposta('<think>pensando...</think>{"a": 1}'))
    assert await LlmClient(BASE, "qwen").chat_json("s", "u") == {"a": 1}


@pytest.mark.parametrize("conteudo", ["nao e json", "[1, 2]", ""])
@respx.mock
async def test_saida_que_nao_e_objeto_json_vira_llm_indisponivel(conteudo: str) -> None:
    respx.post(f"{BASE}/chat/completions").mock(return_value=_resposta(conteudo))
    with pytest.raises(LLMUnavailable):
        await LlmClient(BASE, "qwen").chat_json("s", "u")


@respx.mock
async def test_erro_http_vira_llm_indisponivel() -> None:
    respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(500))
    with pytest.raises(LLMUnavailable):
        await LlmClient(BASE, "qwen").chat_json("s", "u")
