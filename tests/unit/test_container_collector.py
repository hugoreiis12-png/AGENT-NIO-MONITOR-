"""Wiring do cliente Portainer (T6) com o resto do pipeline.

O cliente em si ja tem `test_sources.py::TestClientePortainer`. Aqui o que se
trava e a TRANSICAO de estado: container que estava rodando e parou, container
que ficou unhealthy -- e que nada dispara quando nada mudou.
"""

from __future__ import annotations

import httpx
import respx

from agent_nio_monitor.sources.portainer.client import PortainerClient
from agent_nio_monitor.sources.portainer.collector import ContainerCollector
from agent_nio_monitor.store.repository import InMemoryRepository

PTAI = "https://portainer.test"


def _mock_endpoints() -> None:
    respx.get(f"{PTAI}/api/endpoints").mock(return_value=httpx.Response(200, json=[{"Id": 1}]))


def _mock_containers(containers: list[dict]) -> None:
    respx.get(f"{PTAI}/api/endpoints/1/docker/containers/json").mock(
        return_value=httpx.Response(200, json=containers)
    )


class TestChecagemDeEstado:
    @respx.mock
    async def test_primeira_observacao_nao_gera_evento(self) -> None:
        """Sem historico para comparar, 'esta parado' nao e TRANSICAO."""
        _mock_endpoints()
        _mock_containers([{"Id": "abc", "Names": ["/worker"], "State": "exited",
                            "Status": "Exited (1)"}])
        col = ContainerCollector(PortainerClient(PTAI, "tok"), InMemoryRepository())
        resultado = await col.check_state()
        assert resultado.extra["eventos"] == []

    @respx.mock
    async def test_container_que_parou_gera_evento_critico(self) -> None:
        repo = InMemoryRepository()
        col = ContainerCollector(PortainerClient(PTAI, "tok"), repo)
        _mock_endpoints()
        _mock_containers([{"Id": "abc", "Names": ["/worker"], "State": "running",
                            "Status": "Up 2 hours"}])
        await col.check_state()

        _mock_containers([{"Id": "abc", "Names": ["/worker"], "State": "exited",
                            "Status": "Exited (1)"}])
        resultado = await col.check_state()
        eventos = resultado.extra["eventos"]
        assert len(eventos) == 1
        assert eventos[0].event_type == "container_stopped"
        assert eventos[0].severity == "critical"
        assert eventos[0].resource_id == "abc"

    @respx.mock
    async def test_container_que_fica_unhealthy_gera_evento(self) -> None:
        repo = InMemoryRepository()
        col = ContainerCollector(PortainerClient(PTAI, "tok"), repo)
        _mock_endpoints()
        _mock_containers([{"Id": "abc", "Names": ["/worker"], "State": "running",
                            "Status": "Up 2 hours (healthy)"}])
        await col.check_state()

        _mock_containers([{"Id": "abc", "Names": ["/worker"], "State": "running",
                            "Status": "Up 2 hours (unhealthy)"}])
        resultado = await col.check_state()
        eventos = resultado.extra["eventos"]
        assert len(eventos) == 1
        assert eventos[0].event_type == "container_unhealthy"

    @respx.mock
    async def test_sem_mudanca_nao_gera_evento(self) -> None:
        repo = InMemoryRepository()
        col = ContainerCollector(PortainerClient(PTAI, "tok"), repo)
        _mock_endpoints()
        _mock_containers([{"Id": "abc", "Names": ["/worker"], "State": "running",
                            "Status": "Up 2 hours"}])
        await col.check_state()
        resultado = await col.check_state()
        assert resultado.extra["eventos"] == []

    @respx.mock
    async def test_endpoint_sem_container_nao_quebra(self) -> None:
        _mock_endpoints()
        _mock_containers([])
        col = ContainerCollector(PortainerClient(PTAI, "tok"), InMemoryRepository())
        resultado = await col.check_state()
        assert resultado.collected == 0
        assert resultado.ok
