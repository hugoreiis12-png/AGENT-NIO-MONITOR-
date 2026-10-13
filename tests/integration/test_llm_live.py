"""Teste do redator contra o Ollama REAL (192.168.0.140:11434).

Nao mock. O objetivo e verificar o que mock nao verifica: que um modelo de 8B
produz texto utilizavel, dentro do tempo aceitavel, e que a queda do LLM nao
impede o alerta.

Se o endpoint nao responder, o teste pula -- e o log diz que pulou, para ninguem
achar que passou.
"""

from __future__ import annotations

import os
import time

import pytest

from agent_nio_monitor.llm.writer import AlertWriter, LlmClient, LLMUnavailable

OLLAMA = os.environ.get("OLLAMA_URL", "http://192.168.0.140:11434")
MODELO = os.environ.get("OLLAMA_MODEL", "hermes3:8b")


@pytest.fixture
async def client():
    """Escopo de FUNCAO, nao de modulo: o pytest-asyncio cria um event loop por
    teste, e um httpx.AsyncClient de escopo de modulo fica preso ao loop morto."""
    if os.environ.get("SKIP_LLM_TESTS") == "1":
        pytest.skip("SKIP_LLM_TESTS=1")
    c = LlmClient(OLLAMA, MODELO, timeout=90.0, max_tokens=250)
    if not await c.healthy():
        await c.aclose()
        pytest.skip(f"Ollama indisponivel em {OLLAMA}")
    yield c
    await c.aclose()


class TestLlmReal:
    async def test_modelo_esta_disponivel(self, client: LlmClient) -> None:
        assert await client.healthy()

    async def test_gera_texto_nao_vazio(self, client: LlmClient) -> None:
        texto = await client.generate("Escreva uma frase confirmando que esta online.")
        assert texto.strip()
        assert len(texto) > 10

    async def test_latencia_ficou_dentro_do_aceitavel(self, client: LlmClient) -> None:
        """Medido no curl do dono: ~88 ms/token. Um alerta de ~150 tokens tem de
        caber em poucos segundos, senao o alerta chega depois do incidente."""
        inicio = time.perf_counter()
        await client.generate("Explique em uma frase o que e um workflow quebrado.")
        decorrido = time.perf_counter() - inicio
        print(f"\nlatencia LLM: {decorrido:.2f}s")
        assert decorrido < 30.0, f"LLM lento demais para o caminho de alerta: {decorrido:.1f}s"

    async def test_prompt_sobre_execucao_real_gera_texto_util(self, client: LlmClient) -> None:
        """Contexto tirado do dump real: 9 falhas em 25h, horario fixo."""
        from agent_nio_monitor.llm.writer import USER

        texto = await client.generate(
            USER.format(
                projeto="V3yqT1MGvf0Ufg17",
                recurso="JqOi9bhDIXzi8wKN",
                severidade="critical",
                n=9,
                primeira="2026-10-11 16:30",
                ultima="2026-10-12 16:30",
                erro="Connection timeout",
                modo="trigger",
            )
        )
        print(f"\n--- saida do modelo ---\n{texto}\n---")
        assert 20 < len(texto) < 1200, f"texto fora do tamanho util: {len(texto)}"
        assert texto.lower() not in ("i'm sorry", "nao posso")
        # o requisito do dono: DETALHADO POREM DIRETO. Isso aqui impede
        # enrolacao -- o modelo esta forbidden a extrapol ar de contexto.
        proibido = ("esperamos", "desejamos", "é importante notar", "vale ressaltar")
        baixo = texto.lower()
        for frase in proibido:
            assert frase not in baixo, f"texto enrolado: contem '{frase}'"

    async def test_nao_inventa_numero(self, client: LlmClient) -> None:
        """O prompt diz explicitamente para nao inventar. Verificamos o caso
        em que nao ha numero disponivel."""
        texto = await client.generate(
            "Contexto: um workflow falhou. Nenhum numero foi fornecido.\n"
            "Escreva uma frase. Nao invente numeros."
        )
        import re

        numeros = re.findall(r"\b\d{2,}\b", texto)
        assert not numeros, f"modelo inventou numero: {numeros} em '{texto}'"


class TestDegradacao:
    """O que mais importa: LLM fora, alerta ainda sai."""

    class _ClientQuebrado(LlmClient):
        async def generate(self, prompt: str, *, system: str = "") -> str:
            raise LLMUnavailable("conexao recusada")

        async def healthy(self) -> bool:
            return False

    async def test_llm_quebrado_nao_impede_o_alerta(self) -> None:
        c = self._ClientQuebrado(OLLAMA, MODELO)
        w = AlertWriter(c)
        abertura, status = await w.write(
            {
                "workflow_nome": "Processa Checkout",
                "n": 5,
                "primeira": "2026-10-12 08:00",
                "erro": "Connection timeout",
            }
        )
        assert status == "fallback"
        assert "Processa Checkout" in abertura
        assert "5" in abertura

    async def test_llm_desligado_usa_fallback(self) -> None:
        w = AlertWriter(self._ClientQuebrado(OLLAMA, MODELO), enabled=False)
        abertura, status = await w.write({"workflow_nome": "X", "n": 1})
        assert status == "desligado"
        assert abertura

    async def test_saida_do_llm_nao_pode_vazar_prompt(self) -> None:
        """Um 8B pode ecoar o prompt. Isso nao pode virar assunto de email."""
        class Eco(LlmClient):
            async def generate(self, prompt: str, *, system: str = "") -> str:
                return prompt

        texto, status = await AlertWriter(Eco(OLLAMA, MODELO)).write(
            {"workflow_nome": "X", "n": 1, "erro": "e"}
        )
        assert status == "llm"
        assert "Voce escreve o paragrafo" not in texto, "o modelo ecoou o system prompt"