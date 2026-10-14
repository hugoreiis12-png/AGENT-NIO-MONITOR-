"""Wiring do diff estrutural (T5) com o resto do pipeline.

`diff.py` em si ja tem `test_diff.py`. Aqui o que se trava e o CICLO: cache de
`active` na primeira passada, e snapshot anterior/novo no diff completo.
"""

from __future__ import annotations

import httpx
import respx

from agent_nio_monitor.sources.n8n.client import N8NClient
from agent_nio_monitor.sources.n8n.workflow_monitor import WorkflowMonitor
from agent_nio_monitor.store.memory_repository import InMemoryRepository

N8N = "http://n8n.test"


def _mock_workflows(lista: list[dict]) -> None:
    respx.get(f"{N8N}/api/v1/workflows").mock(
        return_value=httpx.Response(200, json={"data": lista, "nextCursor": None})
    )


def _wf(wid: str, active: bool, **extra) -> dict:
    base = {"id": wid, "name": f"wf-{wid}", "active": active, "nodes": [], "connections": {}}
    base.update(extra)
    return base


class TestToggleDeEstado:
    """Ativar/desativar sai do mesmo diff completo (uma leitura so por ciclo)."""

    @respx.mock
    async def test_primeira_passada_nao_gera_evento_de_ativacao(self) -> None:
        """Sem isso, todo workflow existente pareceria 'acabou de ativar' no
        boot do agente -- falso positivo em massa a cada restart."""
        _mock_workflows([_wf("1", True), _wf("2", False)])
        mon = WorkflowMonitor(N8NClient(N8N, "k", verify_ssl=False), InMemoryRepository())
        resultado = await mon.check_structural_diff()
        assert resultado.extra["eventos"] == []

    @respx.mock
    async def test_ativacao_vira_evento_proprio_sem_duplicar_no_changed(self) -> None:
        mon = WorkflowMonitor(N8NClient(N8N, "k", verify_ssl=False), InMemoryRepository())
        _mock_workflows([_wf("1", False)])
        await mon.check_structural_diff()

        _mock_workflows([_wf("1", True)])
        eventos = (await mon.check_structural_diff()).extra["eventos"]
        assert [e.event_type for e in eventos] == ["workflow_activated"]
        assert eventos[0].resource_id == "1"
        assert eventos[0].detail["ativo"] is True

    @respx.mock
    async def test_desativacao_registra_o_novo_estado(self) -> None:
        mon = WorkflowMonitor(N8NClient(N8N, "k", verify_ssl=False), InMemoryRepository())
        _mock_workflows([_wf("1", True)])
        await mon.check_structural_diff()
        _mock_workflows([_wf("1", False)])
        eventos = (await mon.check_structural_diff()).extra["eventos"]
        assert [e.event_type for e in eventos] == ["workflow_deactivated"]
        assert eventos[0].detail["ativo"] is False

    @respx.mock
    async def test_ativar_e_editar_ao_mesmo_tempo_gera_dois_eventos(self) -> None:
        mon = WorkflowMonitor(N8NClient(N8N, "k", verify_ssl=False), InMemoryRepository())
        _mock_workflows([_wf("1", False, nodes=[{"id": "a", "name": "A", "type": "t"}])])
        await mon.check_structural_diff()
        _mock_workflows([_wf("1", True, nodes=[{"id": "a", "name": "A", "type": "t"},
                                                 {"id": "b", "name": "B", "type": "t"}])])
        eventos = (await mon.check_structural_diff()).extra["eventos"]
        assert sorted(e.event_type for e in eventos) == ["workflow_activated", "workflow_changed"]

    @respx.mock
    async def test_sem_mudanca_nao_gera_evento(self) -> None:
        mon = WorkflowMonitor(N8NClient(N8N, "k", verify_ssl=False), InMemoryRepository())
        _mock_workflows([_wf("1", True)])
        await mon.check_structural_diff()
        assert (await mon.check_structural_diff()).extra["eventos"] == []


class TestDiffEstrutural:
    @respx.mock
    async def test_primeira_vez_grava_snapshot_sem_evento(self) -> None:
        """Nao ha 'antes' pra comparar -- nao pode inventar diff do nada."""
        repo = InMemoryRepository()
        mon = WorkflowMonitor(N8NClient(N8N, "k", verify_ssl=False), repo)
        _mock_workflows([_wf("1", True, nodes=[{"id": "a", "name": "A", "type": "t"}])])

        resultado = await mon.check_structural_diff()
        assert resultado.collected == 0
        assert repo.last_workflow_snapshot("1") is not None

    @respx.mock
    async def test_segunda_vez_detecta_node_removido(self) -> None:
        repo = InMemoryRepository()
        mon = WorkflowMonitor(N8NClient(N8N, "k", verify_ssl=False), repo)
        _mock_workflows([
            _wf("1", True, nodes=[
                {"id": "a", "name": "A", "type": "t"},
                {"id": "b", "name": "B", "type": "t"},
            ])
        ])
        await mon.check_structural_diff()

        _mock_workflows([_wf("1", True, nodes=[{"id": "a", "name": "A", "type": "t"}])])
        resultado = await mon.check_structural_diff()
        eventos = resultado.extra["eventos"]
        assert len(eventos) == 1
        assert eventos[0].event_type == "workflow_changed"
        assert any(c.kind == "node_removed" for c in eventos[0].changes)
        assert "node REMOVIDO" in eventos[0].detail["resumo_diff"]

    @respx.mock
    async def test_snapshot_mais_novo_vira_a_base_do_proximo_diff(self) -> None:
        """Sem atualizar o snapshot a cada ciclo, o diff do ciclo 3 compararia
        contra o estado do ciclo 1 e repetiria uma mudanca ja relatada."""
        repo = InMemoryRepository()
        mon = WorkflowMonitor(N8NClient(N8N, "k", verify_ssl=False), repo)
        _mock_workflows([_wf("1", True, nodes=[{"id": "a", "name": "A", "type": "t"}])])
        await mon.check_structural_diff()

        _mock_workflows([_wf("1", True, nodes=[
            {"id": "a", "name": "A", "type": "t"}, {"id": "b", "name": "B", "type": "t"},
        ])])
        r2 = await mon.check_structural_diff()
        assert len(r2.extra["eventos"]) == 1

        r3 = await mon.check_structural_diff()
        assert r3.collected == 0, "nada mudou desde o ciclo 2 -- nao pode repetir o evento"


class TestWorkflowCriado:
    @respx.mock
    async def test_workflow_novo_depois_da_linha_de_base_gera_evento(self) -> None:
        repo = InMemoryRepository()
        mon = WorkflowMonitor(N8NClient(N8N, "k", verify_ssl=False), repo)
        _mock_workflows([_wf("1", True)])
        await mon.check_structural_diff()

        novo = _wf("2", False, nodes=[{"id": "a", "name": "A", "type": "n8n-nodes-base.webhook"}],
                   activeVersion={"authors": "Ana <ana@x.com>"})
        _mock_workflows([_wf("1", True), novo])
        eventos = (await mon.check_structural_diff()).extra["eventos"]
        assert [e.event_type for e in eventos] == ["workflow_created"]
        assert eventos[0].resource_id == "2"
        assert eventos[0].detail["autor"] == "Ana <ana@x.com>"
        assert "1 nodes (webhook)" in eventos[0].detail["resumo_diff"]

    @respx.mock
    async def test_primeira_coleta_de_todos_nao_vira_um_evento_por_workflow(self) -> None:
        mon = WorkflowMonitor(N8NClient(N8N, "k", verify_ssl=False), InMemoryRepository())
        _mock_workflows([_wf("1", True), _wf("2", True), _wf("3", False)])
        assert (await mon.check_structural_diff()).extra["eventos"] == []

    @respx.mock
    async def test_edicao_que_nao_muda_nada_nao_gera_evento(self) -> None:
        mon = WorkflowMonitor(N8NClient(N8N, "k", verify_ssl=False), InMemoryRepository())
        _mock_workflows([_wf("1", True)])
        await mon.check_structural_diff()
        _mock_workflows([_wf("1", True, versionId="v2")])
        assert (await mon.check_structural_diff()).extra["eventos"] == []


def N8N_CLIENT() -> N8NClient:
    return N8NClient(N8N, "k", verify_ssl=False)


class TestSnapshotSoQuandoMuda:
    @respx.mock
    async def test_ciclos_sem_mudanca_nao_gravam_snapshot_novo(self) -> None:
        repo = InMemoryRepository()
        mon = WorkflowMonitor(N8N_CLIENT(), repo)
        _mock_workflows([_wf("1", True)])
        for _ in range(4):
            await mon.check_structural_diff()
        assert len(repo.snapshots["1"]) == 1

    @respx.mock
    async def test_mudanca_grava_e_vira_a_nova_base(self) -> None:
        repo = InMemoryRepository()
        mon = WorkflowMonitor(N8N_CLIENT(), repo)
        _mock_workflows([_wf("1", True, nodes=[{"id": "a", "name": "A", "type": "t"}])])
        await mon.check_structural_diff()
        _mock_workflows([_wf("1", True, nodes=[{"id": "a", "name": "A", "type": "t"},
                                                 {"id": "b", "name": "B", "type": "t"}])])
        await mon.check_structural_diff()
        await mon.check_structural_diff()
        assert len(repo.snapshots["1"]) == 2
