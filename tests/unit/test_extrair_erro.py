"""O erro real do n8n mora em data.resultData.error -- era lido do lugar errado."""

from __future__ import annotations

import httpx
import respx

from agent_nio_monitor.sources.n8n.client import N8NClient
from agent_nio_monitor.sources.n8n.collector import ExecutionErrorCollector, extrair_erro
from agent_nio_monitor.store.repository import InMemoryRepository

N8N = "http://n8n.test"
DETALHE = {
    "id": 7, "status": "error", "workflowId": "wf1", "startedAt": "2026-10-13T12:00:00.000Z",
    "data": {"resultData": {
        "lastNodeExecuted": "HTTP Request",
        "error": {"name": "NodeApiError", "message": "The service refused the connection",
                  "description": "ECONNREFUSED 10.0.0.5:443", "node": {"name": "Consulta SAP"}},
    }},
}


class TestExtrairErro:
    def test_le_mensagem_descricao_e_no_do_resultdata(self) -> None:
        msg, no = extrair_erro(DETALHE)
        assert msg == "The service refused the connection | ECONNREFUSED 10.0.0.5:443"
        assert no == "Consulta SAP"

    def test_sem_erro_cai_no_ultimo_no_executado(self) -> None:
        msg, no = extrair_erro({"data": {"resultData": {"lastNodeExecuted": "Set"}}})
        assert msg is None
        assert no == "Set"

    def test_mensagem_gigante_e_truncada(self) -> None:
        msg, _ = extrair_erro({"data": {"resultData": {"error": {"message": "x" * 5000}}}})
        assert msg is not None and len(msg) <= 600


@respx.mock
async def test_coletor_devolve_o_ultimo_erro_por_workflow() -> None:
    respx.get(f"{N8N}/api/v1/executions").mock(return_value=httpx.Response(
        200, json={"data": [{"id": 7, "status": "error", "workflowId": "wf1",
                              "startedAt": "2026-10-13T12:00:00.000Z"}], "nextCursor": None}))
    respx.get(f"{N8N}/api/v1/executions/7").mock(return_value=httpx.Response(200, json=DETALHE))
    col = ExecutionErrorCollector(N8NClient(N8N, "k", verify_ssl=False), InMemoryRepository())
    resultado = await col.run()
    ultimo = resultado.extra["ultimos_erros"]["wf1"]
    assert ultimo.error_msg.startswith("The service refused")
    assert ultimo.last_node == "Consulta SAP"
