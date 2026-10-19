"""Cliente do LLM local (Qwen no vLLM, API compativel com OpenAI).

REGRA (ADR D3): o LLM analisa e redige. Nao decide severidade, destinatario nem
se ha alerta. Se ele cair, o alerta sai do mesmo jeito (fallback deterministico).
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMUnavailable(RuntimeError):
    pass


class LlmClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = 60.0,
        temperature: float = 0.2,
        max_tokens: int = 700,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat_json(self, system: str, user: str) -> dict[str, Any]:
        """Uma pergunta, uma resposta JSON. Sem "thinking": mede ~2 s contra ~6 s
        e o raciocinio nao entra no e-mail de qualquer jeito."""
        return await self.chat_json_messages(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )

    async def chat_json_messages(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        mensagem = await self._chat({"messages": messages, "response_format": {"type": "json_object"}})
        try:
            dados = json.loads(THINK_RE.sub("", mensagem.get("content") or "").strip())
        except ValueError as exc:
            raise LLMUnavailable(f"JSON invalido: {exc}") from exc
        if not isinstance(dados, dict):
            raise LLMUnavailable("LLM devolveu JSON que nao e objeto")
        return dados

    async def chat_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        """Uma rodada com ferramentas: a mensagem do assistente, com ou sem `tool_calls`.

        Verificado em 19/10/2026: o vLLM deste Qwen ja sobe com o parser de tool call
        no formato Hermes -- a chamada volta estruturada em `tool_calls`."""
        return await self._chat({"messages": messages, "tools": tools, "tool_choice": "auto"})

    async def _chat(self, extra: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "model": self.model, "temperature": self.temperature, "max_tokens": self.max_tokens,
            "chat_template_kwargs": {"enable_thinking": False}, **extra,
        }
        try:
            resp = await self._client.post(f"{self.base_url}/chat/completions", json=payload)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc

    async def healthy(self) -> bool:
        try:
            resp = await self._client.get(f"{self.base_url}/models", timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
