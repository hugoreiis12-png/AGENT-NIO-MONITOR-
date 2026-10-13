"""O projeto do workflow (shared[].projectId) e o que decide quem recebe o alerta."""

from __future__ import annotations

import httpx
import respx

from agent_nio_monitor.sources.n8n.client import N8NClient
from agent_nio_monitor.sources.n8n.workflow_index import WorkflowIndex

N8N = "http://n8n.test"


@respx.mock
async def test_indexa_nome_e_projeto() -> None:
    respx.get(f"{N8N}/api/v1/workflows").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "1", "name": "A", "shared": [{"projectId": "P1"}]},
                    {"id": "2", "name": "B", "shared": []},
                ],
                "nextCursor": None,
            },
        )
    )
    index = WorkflowIndex(N8NClient(N8N, "k", verify_ssl=False))
    await index.load()
    assert index.names == {"1": "A", "2": "B"}
    assert index.projects == {"1": "P1"}


@respx.mock
async def test_falha_nao_trava_o_cache_e_tenta_de_novo() -> None:
    rota = respx.get(f"{N8N}/api/v1/workflows").mock(return_value=httpx.Response(500))
    index = WorkflowIndex(N8NClient(N8N, "k", verify_ssl=False, ))
    await index.load()
    assert index.names == {}
    rota.mock(return_value=httpx.Response(200, json={"data": [{"id": "1", "name": "A"}], "nextCursor": None}))
    await index.load()
    assert index.names == {"1": "A"}
