"""Analista contra o Qwen REAL (vLLM em 192.168.0.140:8001).

Nao e mock. Verifica o que mock nao verifica: que o modelo devolve JSON no formato
esperado, dentro do tempo aceitavel, sem inventar numero, e que a queda do LLM nao
impede o alerta. Se o endpoint nao responder, pula -- e o log diz que pulou.
"""

from __future__ import annotations

import os
import time

import pytest

from agent_nio_monitor.llm.analyst import AlertAnalyst
from agent_nio_monitor.llm.client import LlmClient

QWEN_URL = os.environ.get("QWEN_URL", "http://192.168.0.140:8001/v1")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "RedHatAI/Qwen3.8-27B-INT4")
LIMITE_SEGUNDOS = 30.0

EXECUCAO = {
    "tipo": "execution_failed", "titulo": "SITE GR API.V2 falhou 3x", "severidade": "high",
    "workflow_nome": "SITE GR API.V2", "ocorrencias": 3, "no": "Consulta SAP",
    "erro": "The service refused the connection | ECONNREFUSED 10.0.0.5:443",
    "autor": "Rafael Barboza",
}
CONTAINER = {
    "tipo": "container_stopped", "titulo": "Container parado: worker", "severidade": "critical",
    "recurso": "worker", "stack": "comprove", "detalhe": "Estado atual: exited (Exited (1))",
    "logs": "Error: connect ECONNREFUSED 10.0.0.5:5432\n    at TCPConnectWrap.afterConnect",
}


@pytest.fixture
async def client():
    """Escopo de FUNCAO: o pytest-asyncio cria um event loop por teste."""
    if os.environ.get("SKIP_LLM_TESTS") == "1":
        pytest.skip("SKIP_LLM_TESTS=1")
    c = LlmClient(QWEN_URL, QWEN_MODEL, timeout=90.0)
    if not await c.healthy():
        await c.aclose()
        pytest.skip(f"Qwen indisponivel em {QWEN_URL}")
    yield c
    await c.aclose()


class TestQwenReal:
    @pytest.mark.parametrize("contexto", [EXECUCAO, CONTAINER], ids=["execucao", "container"])
    async def test_analise_util_no_formato_esperado(self, client: LlmClient, contexto: dict) -> None:
        inicio = time.perf_counter()
        analise, status = await AlertAnalyst(client).analyze(contexto)
        decorrido = time.perf_counter() - inicio
        print(f"\n[{status}] {decorrido:.1f}s -> {analise}")
        assert decorrido < LIMITE_SEGUNDOS, f"LLM lento demais para o alerta: {decorrido:.1f}s"
        assert status == "llm", "o Qwen deveria devolver analise valida (ou o guard rejeitou)"
        assert analise is not None
        assert 2 <= len(analise.como_resolver) <= 5

    async def test_sem_causa_nos_fatos_o_modelo_nao_inventa(self, client: LlmClient) -> None:
        analise, _ = await AlertAnalyst(client).analyze(
            {"tipo": "workflow_changed", "titulo": "wf foi alterado", "severidade": "medium",
             "workflow_nome": "wf", "resumo_diff": "node REMOVIDO: Set"}
        )
        if analise is not None:
            assert "2023" not in analise.resumo + analise.causa_provavel


class TestDegradacao:
    async def test_llm_fora_do_ar_nao_impede_o_alerta(self) -> None:
        c = LlmClient("http://127.0.0.1:9/v1", "x", timeout=2.0)
        assert await AlertAnalyst(c).analyze(EXECUCAO) == (None, "fallback")
        await c.aclose()
