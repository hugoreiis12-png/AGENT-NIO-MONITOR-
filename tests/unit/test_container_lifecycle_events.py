"""Container removido, loop de reinicio e consumo sustentado, ponta a ponta no coletor."""

from __future__ import annotations

import httpx
import respx

from agent_nio_monitor.sources.portainer.client import PortainerClient
from agent_nio_monitor.sources.portainer.collector import ContainerCollector
from agent_nio_monitor.sources.portainer.stats import ResourceCollector
from agent_nio_monitor.store.memory_repository import InMemoryRepository

PTAI = "https://portainer.test"
LISTA = f"{PTAI}/api/endpoints/1/docker/containers/json"
STACKS = [{"Name": "comprove", "CreatedBy": "admin", "UpdatedBy": "carlos"}]


def _base() -> None:
    respx.get(f"{PTAI}/api/endpoints").mock(return_value=httpx.Response(200, json=[{"Id": 1}]))
    respx.get(f"{PTAI}/api/stacks").mock(return_value=httpx.Response(200, json=STACKS))
    respx.get(url__regex=rf"{PTAI}/api/endpoints/1/docker/containers/.+/logs").mock(
        return_value=httpx.Response(200, content=b"")
    )


def _linha(nome: str, status: str = "Up 4 weeks", estado: str = "running") -> dict:
    return {"Id": f"id-{nome}", "Names": [f"/{nome}"], "State": estado, "Status": status,
            "Labels": {"com.docker.compose.project": "comprove"}}


def _lista(*linhas: dict) -> None:
    respx.get(LISTA).mock(return_value=httpx.Response(200, json=list(linhas)))


class TestContainerRemovido:
    @respx.mock
    async def test_sumiu_por_duas_coletas_alerta_com_o_dono_da_stack(self) -> None:
        _base()
        col = ContainerCollector(PortainerClient(PTAI, "tok"), InMemoryRepository())
        _lista(_linha("api"), _linha("worker"))
        await col.check_state()
        _lista(_linha("api"))
        assert (await col.check_state()).extra["eventos"] == []
        eventos = (await col.check_state()).extra["eventos"]
        assert [e.event_type for e in eventos] == ["container_removed"]
        assert eventos[0].detail["recurso"] == "worker"
        assert eventos[0].detail["autor"] == "carlos"

    @respx.mock
    async def test_container_recriado_com_novo_id_nao_e_remocao(self) -> None:
        _base()
        col = ContainerCollector(PortainerClient(PTAI, "tok"), InMemoryRepository())
        _lista(_linha("api"))
        await col.check_state()
        recriado = {**_linha("api"), "Id": "id-novo", "Status": "Up 3 seconds"}
        respx.get(url__regex=rf"{PTAI}/api/endpoints/1/docker/containers/.+/json").mock(
            return_value=httpx.Response(200, json={"RestartCount": 0}))
        _lista(recriado)
        for _ in range(3):
            eventos = (await col.check_state()).extra["eventos"]
            assert [e for e in eventos if e.event_type == "container_removed"] == []


class TestLoopDeReinicio:
    @respx.mock
    async def test_reinicios_em_sequencia_geram_um_alerta(self) -> None:
        _base()
        col = ContainerCollector(
            PortainerClient(PTAI, "tok"), InMemoryRepository(), loop_count=3, loop_window_minutes=10)
        contagem = {"n": 0}

        def inspect(_request: httpx.Request) -> httpx.Response:
            contagem["n"] += 2
            return httpx.Response(200, json={"RestartCount": contagem["n"]})

        respx.get(url__regex=rf"{PTAI}/api/endpoints/1/docker/containers/id-api/json").mock(side_effect=inspect)
        _lista(_linha("api", status="Restarting (1) 3 seconds ago", estado="restarting"))
        tipos = []
        for _ in range(3):
            tipos += [e.event_type for e in (await col.check_state()).extra["eventos"]]
        assert tipos.count("container_restart_loop") == 1

    @respx.mock
    async def test_container_estavel_nunca_e_inspecionado(self) -> None:
        _base()
        col = ContainerCollector(PortainerClient(PTAI, "tok"), InMemoryRepository())
        inspect = respx.get(url__regex=rf"{PTAI}/api/endpoints/1/docker/containers/.+/json").mock(
            return_value=httpx.Response(200, json={"RestartCount": 0}))
        _lista(_linha("api"), _linha("worker"))
        await col.check_state()
        assert inspect.call_count == 0


def _stats(uso: int, sistema: int, mem: int = 100, limite: int = 1000) -> dict:
    return {"cpu_stats": {"cpu_usage": {"total_usage": uso}, "system_cpu_usage": sistema},
            "memory_stats": {"usage": mem, "limit": limite, "stats": {}}}


class TestConsumoSustentado:
    def _rota_stats(self, respostas: list[dict]) -> None:
        fila = iter(respostas)
        respx.get(url__regex=rf"{PTAI}/api/endpoints/1/docker/containers/.+/stats").mock(
            side_effect=lambda _r: httpx.Response(200, json=next(fila)))

    @respx.mock
    async def test_memoria_alta_por_tres_coletas_alerta_uma_vez(self) -> None:
        _base()
        col = ResourceCollector(PortainerClient(PTAI, "tok"), InMemoryRepository(), mem_pct=90, sustained_samples=3)
        _lista(_linha("api"))
        self._rota_stats([_stats(0, 0, mem=950)] * 5)
        tipos = []
        for _ in range(5):
            tipos += [e.event_type for e in (await col.check_resources()).extra["eventos"]]
        assert tipos == ["container_resource_high"]

    @respx.mock
    async def test_pico_isolado_nao_alerta(self) -> None:
        _base()
        col = ResourceCollector(PortainerClient(PTAI, "tok"), InMemoryRepository(), mem_pct=90, sustained_samples=3)
        _lista(_linha("api"))
        self._rota_stats([_stats(0, 0, mem=m) for m in (950, 950, 100, 950, 950, 100)])
        for _ in range(6):
            assert (await col.check_resources()).extra["eventos"] == []

    @respx.mock
    async def test_cpu_e_media_entre_coletas_e_a_primeira_so_forma_base(self) -> None:
        _base()
        col = ResourceCollector(PortainerClient(PTAI, "tok"), InMemoryRepository(), cpu_host_pct=80, sustained_samples=2)
        _lista(_linha("api"))
        amostras = [_stats(0, 0), _stats(900, 1000), _stats(1800, 2000)]
        self._rota_stats(amostras)
        tipos = []
        for _ in range(3):
            tipos += [e.event_type for e in (await col.check_resources()).extra["eventos"]]
        assert tipos == ["container_resource_high"]

    @respx.mock
    async def test_alerta_leva_stack_e_autor(self) -> None:
        _base()
        col = ResourceCollector(PortainerClient(PTAI, "tok"), InMemoryRepository(), mem_pct=90, sustained_samples=1)
        _lista(_linha("api"))
        self._rota_stats([_stats(0, 0, mem=990)])
        evento = (await col.check_resources()).extra["eventos"][0]
        assert evento.detail["stack"] == "comprove"
        assert evento.detail["autor"] == "carlos"
        assert "memoria em 99%" in evento.detail["detalhe"]