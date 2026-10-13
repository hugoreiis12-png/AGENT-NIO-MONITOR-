"""Mudanca de stack: so alerta refactor acima de 20% do compose."""

from __future__ import annotations

import httpx
import respx

from agent_nio_monitor.sources.portainer.client import PortainerClient
from agent_nio_monitor.sources.portainer.stacks import StackCollector
from agent_nio_monitor.store.repository import InMemoryRepository

PTAI = "https://portainer.test"

COMPOSE = """services:
  api:
    image: app/api:1.0
    environment:
      DB_PASSWORD: hunter2
  worker:
    image: app/worker:1.0
  redis:
    image: redis:7
  cache:
    image: memcached:1
"""


def _stack(update: int = 1, por: str = "carlos", nome: str = "comprove") -> dict:
    return {"Id": 7, "Name": nome, "CreatedBy": "admin", "UpdatedBy": por, "UpdateDate": update}


def _mock(stack: dict, compose: str) -> None:
    respx.get(f"{PTAI}/api/stacks").mock(return_value=httpx.Response(200, json=[stack]))
    respx.get(f"{PTAI}/api/stacks/7/file").mock(
        return_value=httpx.Response(200, json={"StackFileContent": compose})
    )


def _coletor(repo: InMemoryRepository, ratio: float = 0.20) -> StackCollector:
    return StackCollector(PortainerClient(PTAI, "tok"), repo, ratio_minimo=ratio)


class TestMudancaDeStack:
    @respx.mock
    async def test_primeira_coleta_so_forma_a_linha_de_base(self) -> None:
        _mock(_stack(), COMPOSE)
        resultado = await _coletor(InMemoryRepository()).check_changes()
        assert resultado.extra["eventos"] == []

    @respx.mock
    async def test_sem_mudanca_de_assinatura_nem_busca_o_arquivo(self) -> None:
        repo = InMemoryRepository()
        _mock(_stack(), COMPOSE)
        await _coletor(repo).check_changes()
        arquivo = respx.get(f"{PTAI}/api/stacks/7/file")
        chamadas = arquivo.call_count
        await _coletor(repo).check_changes()
        assert arquivo.call_count == chamadas

    @respx.mock
    async def test_ajuste_pequeno_abaixo_de_20_por_cento_nao_alerta(self) -> None:
        repo = InMemoryRepository()
        _mock(_stack(update=1), COMPOSE)
        await _coletor(repo).check_changes()
        _mock(_stack(update=2), COMPOSE.replace("app/api:1.0", "app/api:1.1"))
        assert (await _coletor(repo).check_changes()).extra["eventos"] == []

    @respx.mock
    async def test_refactor_acima_de_20_por_cento_alerta_com_autor_e_resumo(self) -> None:
        repo = InMemoryRepository()
        _mock(_stack(update=1), COMPOSE)
        await _coletor(repo).check_changes()
        reduzido = "services:\n  api:\n    image: app/api:2.0\n"
        _mock(_stack(update=2, por="felipe"), reduzido)
        eventos = (await _coletor(repo).check_changes()).extra["eventos"]
        assert len(eventos) == 1
        detalhe = eventos[0].detail
        assert eventos[0].event_type == "stack_changed"
        assert detalhe["autor"] == "felipe"
        assert detalhe["responsavel_stack"] == "admin"
        assert "removidos: cache, redis, worker" in detalhe["resumo_diff"]

    @respx.mock
    async def test_snapshot_guardado_nao_tem_o_segredo(self) -> None:
        repo = InMemoryRepository()
        _mock(_stack(), COMPOSE)
        await _coletor(repo).check_changes()
        guardado = repo.list_resources("portainer", "stack")[0]["meta"]["content"]
        assert "hunter2" not in guardado

    @respx.mock
    async def test_stack_nova_depois_da_linha_de_base_alerta(self) -> None:
        repo = InMemoryRepository()
        _mock(_stack(), COMPOSE)
        await _coletor(repo).check_changes()
        respx.get(f"{PTAI}/api/stacks").mock(return_value=httpx.Response(200, json=[
            _stack(), {**_stack(nome="nova"), "Id": 9}]))
        respx.get(f"{PTAI}/api/stacks/9/file").mock(
            return_value=httpx.Response(200, json={"StackFileContent": COMPOSE}))
        eventos = (await _coletor(repo).check_changes()).extra["eventos"]
        assert [e.detail["stack"] for e in eventos] == ["nova"]
        assert eventos[0].detail["resumo_diff"] == "Stack nova criada."

    @respx.mock
    async def test_arquivo_indisponivel_tenta_de_novo_no_proximo_ciclo(self) -> None:
        repo = InMemoryRepository()
        _mock(_stack(), COMPOSE)
        await _coletor(repo).check_changes()
        respx.get(f"{PTAI}/api/stacks").mock(return_value=httpx.Response(200, json=[_stack(update=2)]))
        respx.get(f"{PTAI}/api/stacks/7/file").mock(return_value=httpx.Response(500))
        assert (await _coletor(repo).check_changes()).extra["eventos"] == []
        meta = repo.list_resources("portainer", "stack")[0]["meta"]
        assert meta["update_date"] == 1, "snapshot nao pode avancar sem ter lido o arquivo"