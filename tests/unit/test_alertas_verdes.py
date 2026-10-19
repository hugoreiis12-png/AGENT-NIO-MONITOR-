"""Avisos verdes (ADR D44): criacao de workflow, container e fluxo de RPA, e refactor de workflow
com score de risco ate `refactor_max_score`. Severidade baixa = tema verde do e-mail."""

from __future__ import annotations

from agent_nio_monitor.config.loader import load
from agent_nio_monitor.notify.html_render import tema_da_severidade
from agent_nio_monitor.rules.engine import RuleEngine
from agent_nio_monitor.sources.n8n.diff import diff_workflows, risco_para_score
from agent_nio_monitor.sources.portainer.created import CreatedContainers, evento_containers_criados
from agent_nio_monitor.sources.powerautomate.flows import DesktopFlowCatalog
from agent_nio_monitor.store.memory_repository import InMemoryRepository

LIMITE = 1


def _wf(nos: list[dict], conexoes: dict | None = None) -> dict:
    return {"nodes": nos, "connections": conexoes or {}}


def _no(nome: str, tipo: str = "n8n-nodes-base.set", **extra) -> dict:
    return {"id": nome, "name": nome, "type": tipo, **extra}


def _score(antes: dict, depois: dict) -> int:
    return risco_para_score(diff_workflows(antes, depois))[0]


class TestRefactor:
    def test_renomear_e_subir_versao_do_no_e_refactor(self) -> None:
        antes = _wf([_no("A", typeVersion=1)])
        assert _score(antes, _wf([{**_no("A", typeVersion=2), "name": "A renomeado"}])) <= LIMITE

    def test_no_avulso_sem_conexao_e_refactor(self) -> None:
        assert _score(_wf([_no("A")]), _wf([_no("A"), _no("B")])) <= LIMITE

    def test_gatilho_novo_nunca_e_refactor(self) -> None:
        assert _score(_wf([_no("A")]), _wf([_no("A"), _no("T", "n8n-nodes-base.scheduleTrigger")])) > LIMITE

    def test_mudar_o_comportamento_em_erro_nao_e_refactor(self) -> None:
        assert _score(_wf([_no("A")]), _wf([_no("A", onError="continueRegularOutput")])) > LIMITE

    def test_remover_conexao_nao_e_refactor(self) -> None:
        com = {"A": {"main": [[{"node": "B"}]]}}
        assert _score(_wf([_no("A"), _no("B")], com), _wf([_no("A"), _no("B")])) > LIMITE


class TestSeveridadeVerde:
    def test_criacao_e_refactor_saem_verdes_pela_policy(self) -> None:
        regras = RuleEngine(load().policy)
        for tipo in ("workflow_created", "workflow_refactored", "container_created", "desktop_flow_created"):
            decisao = regras.evaluate(event_type=tipo)
            assert decisao.notify and decisao.severity == "low", tipo
            assert tema_da_severidade(decisao.severity).titulo == "Tudo certo!", tipo


def _row(nome: str, stack: str | None = None) -> dict:
    labels = {"com.docker.compose.project": stack} if stack else {}
    return {"Id": nome * 3, "Names": [f"/{nome}"], "Image": f"img/{nome}", "State": "running", "Labels": labels}


class TestContainerCriado:
    def test_primeira_vista_do_endpoint_so_semeia(self) -> None:
        assert CreatedContainers(InMemoryRepository()).novos(3, [_row("api"), _row("db")]) == {}

    def test_containers_da_mesma_stack_viram_um_aviso(self) -> None:
        criados = CreatedContainers(InMemoryRepository())
        criados.novos(3, [_row("api")])
        grupos = criados.novos(3, [_row("api"), _row("web", "loja"), _row("fila", "loja")])
        assert list(grupos) == ["loja"] and len(grupos["loja"]) == 2
        evento = evento_containers_criados(3, "loja", grupos["loja"], {})
        assert evento.event_type == "container_created" and evento.detail["containers"] == ["fila", "web"]

    def test_recriar_com_o_mesmo_nome_nao_e_criacao(self) -> None:
        repo = InMemoryRepository()
        CreatedContainers(repo).novos(3, [_row("api")])
        assert CreatedContainers(repo).novos(3, [{**_row("api"), "Id": "outro-id"}]) == {}


class DataverseFluxos:
    def __init__(self, fluxos: list[dict]) -> None:
        self.fluxos = fluxos

    async def get(self, recurso: str, params: dict, anotacoes: bool = False) -> dict:
        assert recurso == "workflows" and params["$filter"] == "category eq 6"
        return {"value": self.fluxos}


class TestFluxoRpaCriado:
    async def test_semeia_e_depois_avisa_o_novo(self) -> None:
        repo = InMemoryRepository()
        existentes = [{"workflowid": "f1", "name": "CONTAS-A-RECEBER"}]
        assert await DesktopFlowCatalog(DataverseFluxos(existentes), repo).novos() == []
        novo = {"workflowid": "f2", "name": "CONCILIACAO", "createdon": "2026-10-19T12:00:00Z",
                "_ownerid_value@OData.Community.Display.V1.FormattedValue": "Rafael Barboza"}
        eventos = await DesktopFlowCatalog(DataverseFluxos([*existentes, novo]), repo).novos()
        assert [e.event_type for e in eventos] == ["desktop_flow_created"]
        assert eventos[0].resource_id == "pa:f2" and eventos[0].detail["responsavel_fluxo"] == "Rafael Barboza"
