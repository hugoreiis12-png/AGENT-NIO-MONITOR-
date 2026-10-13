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
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            resp = await self._client.post(f"{self.base_url}/chat/completions", json=payload)
            resp.raise_for_status()
            texto = resp.json()["choices"][0]["message"]["content"] or ""
            dados = json.loads(THINK_RE.sub("", texto).strip())
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc
        if not isinstance(dados, dict):
            raise LLMUnavailable("LLM devolveu JSON que nao e objeto")
        return dados

    async def healthy(self) -> bool:
        try:
            resp = await self._client.get(f"{self.base_url}/models", timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
