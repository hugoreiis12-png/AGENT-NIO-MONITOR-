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
from agent_nio_monitor.store.memory_repository import InMemoryRepository

PTAI = "https://portainer.test"


def _mock_endpoints(stacks: list[dict] | None = None) -> None:
    respx.get(f"{PTAI}/api/endpoints").mock(return_value=httpx.Response(200, json=[{"Id": 1}]))
    respx.get(f"{PTAI}/api/stacks").mock(return_value=httpx.Response(200, json=stacks or []))


def _mock_logs(texto: bytes = b"") -> None:
    respx.get(url__regex=rf"{PTAI}/api/endpoints/1/docker/containers/.+/logs").mock(
        return_value=httpx.Response(200, content=texto)
    )


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
        _mock_logs()
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
        _mock_logs()
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

    @respx.mock
    async def test_evento_leva_o_dono_e_o_ultimo_autor_da_stack(self) -> None:
        repo = InMemoryRepository()
        col = ContainerCollector(PortainerClient(PTAI, "tok"), repo)
        _mock_endpoints([{"Name": "comprove", "CreatedBy": "admin", "UpdatedBy": "carlos"}])
        _mock_logs()
        rotulos = {"com.docker.compose.project": "comprove"}
        _mock_containers([{"Id": "abc", "Names": ["/worker"], "State": "running",
                            "Status": "Up", "Labels": rotulos}])
        await col.check_state()
        _mock_containers([{"Id": "abc", "Names": ["/worker"], "State": "exited",
                            "Status": "Exited (1)", "Labels": rotulos}])
        evento = (await col.check_state()).extra["eventos"][0]
        assert evento.detail["stack"] == "comprove"
        assert evento.detail["autor"] == "carlos"
        assert evento.detail["responsavel_stack"] == "admin"

    @respx.mock
    async def test_stacks_fora_do_ar_nao_derruba_o_alerta(self) -> None:
        repo = InMemoryRepository()
        col = ContainerCollector(PortainerClient(PTAI, "tok"), repo)
        _mock_endpoints()
        _mock_logs()
        respx.get(f"{PTAI}/api/stacks").mock(return_value=httpx.Response(500))
        _mock_containers([{"Id": "abc", "Names": ["/w"], "State": "running", "Status": "Up"}])
        await col.check_state()
        _mock_containers([{"Id": "abc", "Names": ["/w"], "State": "exited", "Status": "Exited (1)"}])
        eventos = (await col.check_state()).extra["eventos"]
        assert len(eventos) == 1
        assert "autor" not in eventos[0].detail

    @respx.mock
    async def test_evento_leva_o_log_recente_redigido(self) -> None:
        col = ContainerCollector(PortainerClient(PTAI, "tok"), InMemoryRepository())
        _mock_endpoints()
        quadro = b"connect ECONNREFUSED 10.0.0.5:443 password=hunter2\n"
        _mock_logs(bytes([1, 0, 0, 0]) + len(quadro).to_bytes(4, "big") + quadro)
        _mock_containers([{"Id": "abc", "Names": ["/w"], "State": "running", "Status": "Up"}])
        await col.check_state()
        _mock_containers([{"Id": "abc", "Names": ["/w"], "State": "exited", "Status": "Exited (1)"}])
        evento = (await col.check_state()).extra["eventos"][0]
        assert "ECONNREFUSED" in evento.detail["logs"]
        assert "hunter2" not in evento.detail["logs"]

    @respx.mock
    async def test_log_indisponivel_nao_derruba_o_alerta(self) -> None:
        col = ContainerCollector(PortainerClient(PTAI, "tok"), InMemoryRepository())
        _mock_endpoints()
        respx.get(url__regex=rf"{PTAI}/api/endpoints/1/docker/containers/.+/logs").mock(
            return_value=httpx.Response(500))
        _mock_containers([{"Id": "abc", "Names": ["/w"], "State": "running", "Status": "Up"}])
        await col.check_state()
        _mock_containers([{"Id": "abc", "Names": ["/w"], "State": "exited", "Status": "Exited (1)"}])
        evento = (await col.check_state()).extra["eventos"][0]
        assert evento.detail["logs"] is None
