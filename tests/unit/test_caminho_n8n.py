"""Leitura no a no das execucoes com sucesso (ADR D39): erro escondido e etapa que nao rodou.

Formato das fixtures = o da API real (19/10/2026): `workflowData` + `data.resultData.runData`,
cada entrada com `startTime`, `executionTime` e `data.main` (itens por saida).
"""

from __future__ import annotations

from agent_nio_monitor.sources.n8n.execution_paths import ExecutionPathCollector
from agent_nio_monitor.sources.n8n.node_baseline import MIN_AMOSTRAS, NodeBaseline
from agent_nio_monitor.sources.n8n.node_path import erros_escondidos, por_que_nao_rodou, resumo_do_caminho
from agent_nio_monitor.store.memory_repository import InMemoryRepository


def _no(nome: str, tipo: str = "set", **extra) -> dict:
    return {"name": nome, "type": f"n8n-nodes-base.{tipo}", **extra}


def _liga(*pares: tuple[str, int, str]) -> dict:
    conexoes: dict = {}
    for origem, saida, destino in pares:
        saidas = conexoes.setdefault(origem, {"main": []})["main"]
        while len(saidas) <= saida:
            saidas.append([])
        saidas[saida].append({"node": destino, "type": "main", "index": 0})
    return conexoes


def _rodou(inicio: int, *saidas: int, erro: dict | None = None, itens: list | None = None) -> list[dict]:
    main = [[{"json": {}} for _ in range(n)] for n in saidas]
    if itens is not None:
        main = [itens]
    entrada = {"startTime": inicio, "executionTime": 3, "data": {"main": main}}
    if erro:
        entrada["error"] = erro
    return [entrada]


# Webhook -> IF -> (verdadeiro) Grava -> Responde ; (falso) Avisa
WORKFLOW = {
    "name": "Pedidos",
    "nodes": [_no("Webhook", "webhook"), _no("IF", "if"), _no("Grava", "postgres"),
              _no("Responde", "respondToWebhook"), _no("Avisa", "emailSend"), _no("Nota", "stickyNote")],
    "connections": _liga(("Webhook", 0, "IF"), ("IF", 0, "Grava"), ("IF", 1, "Avisa"), ("Grava", 0, "Responde")),
}
CAMINHO_NORMAL = {"Webhook": _rodou(1, 1), "IF": _rodou(2, 1, 0), "Grava": _rodou(3, 1), "Responde": _rodou(4, 1)}


def _execucao(runs: dict, workflow: dict = WORKFLOW) -> dict:
    return {"workflowData": workflow, "data": {"resultData": {"runData": runs}}}


class TestPorQueNaoRodou:
    def test_if_que_mudou_de_ramo(self) -> None:
        runs = {"Webhook": _rodou(1, 1), "IF": _rodou(2, 0, 1), "Avisa": _rodou(3, 1)}
        assert "'IF' mandou os itens por outra saida" in por_que_nao_rodou(WORKFLOW, runs, "Grava")

    def test_sobe_o_grafo_ate_o_no_que_rodou(self) -> None:
        runs = {"Webhook": _rodou(1, 1), "IF": _rodou(2, 0, 1), "Avisa": _rodou(3, 1)}
        assert "'IF'" in por_que_nao_rodou(WORKFLOW, runs, "Responde")

    def test_no_comum_que_devolveu_zero_itens(self) -> None:
        runs = {**CAMINHO_NORMAL, "Grava": _rodou(3, 0)}
        del runs["Responde"]
        assert "'Grava' devolveu 0 itens" in por_que_nao_rodou(WORKFLOW, runs, "Responde")

    def test_entregou_itens_e_o_seguinte_nao_rodou_e_interrupcao(self) -> None:
        runs = {k: v for k, v in CAMINHO_NORMAL.items() if k != "Responde"}
        assert "interrompida" in por_que_nao_rodou(WORKFLOW, runs, "Responde")


class TestErrosEscondidos:
    def test_erro_gravado_na_entrada_do_no(self) -> None:
        runs = {**CAMINHO_NORMAL, "Grava": _rodou(3, 1, erro={"message": "duplicate key"})}
        assert erros_escondidos(WORKFLOW, runs) == {"Grava": "duplicate key"}

    def test_continue_regular_output_com_erro_no_item(self) -> None:
        workflow = {**WORKFLOW, "nodes": [*WORKFLOW["nodes"][:2], _no("Grava", "postgres", onError="continueRegularOutput"),
                                          *WORKFLOW["nodes"][3:]]}
        runs = {**CAMINHO_NORMAL, "Grava": _rodou(3, itens=[{"json": {"error": {"message": "timeout"}}}])}
        assert erros_escondidos(workflow, runs) == {"Grava": "timeout"}

    def test_item_com_chave_error_sem_continuar_em_erro_nao_e_achado(self) -> None:
        """Um campo `error` no DADO de negocio nao e erro do no."""
        runs = {**CAMINHO_NORMAL, "Grava": _rodou(3, itens=[{"json": {"error": "campo do cliente"}}])}
        assert erros_escondidos(WORKFLOW, runs) == {}

    def test_resumo_lista_na_ordem_com_itens(self) -> None:
        resumo = resumo_do_caminho(CAMINHO_NORMAL)
        assert resumo.splitlines()[0].startswith("1. Webhook (1 item(ns)")
        assert resumo.splitlines()[-1].startswith("4. Responde")


class TestCaminhoNormal:
    def test_nao_espera_nada_enquanto_aprende(self) -> None:
        base = NodeBaseline(InMemoryRepository())
        for _ in range(MIN_AMOSTRAS - 1):
            base.registrar("wf", "Pedidos", set(CAMINHO_NORMAL))
        assert base.esperados("wf", set(CAMINHO_NORMAL)) == set()

    def test_ramo_raro_fica_fora_do_caminho_normal(self) -> None:
        base = NodeBaseline(InMemoryRepository())
        for i in range(40):
            base.registrar("wf", "Pedidos", set(CAMINHO_NORMAL) | ({"Avisa"} if i % 10 == 0 else set()))
        assert base.esperados("wf", {"Webhook", "IF", "Grava", "Responde", "Avisa"}) == set(CAMINHO_NORMAL)

    def test_no_removido_do_workflow_nao_e_esperado(self) -> None:
        base = NodeBaseline(InMemoryRepository())
        for _ in range(MIN_AMOSTRAS):
            base.registrar("wf", "Pedidos", set(CAMINHO_NORMAL))
        assert "Grava" not in base.esperados("wf", {"Webhook", "IF", "Responde"})

    def test_sobrevive_a_reinicio(self) -> None:
        repo = InMemoryRepository()
        for _ in range(MIN_AMOSTRAS):
            NodeBaseline(repo).registrar("wf", "Pedidos", set(CAMINHO_NORMAL))
        assert NodeBaseline(repo).esperados("wf", set(CAMINHO_NORMAL)) == set(CAMINHO_NORMAL)


class N8nFalso:
    def __init__(self) -> None:
        self.paginas: list[dict] = []
        self.detalhes: dict[int, dict] = {}

    async def iter_executions_pages(self, **_kw):  # noqa: ANN003, ANN201
        for pagina in self.paginas:
            yield pagina

    async def get_execution(self, execucao_id, include_data=True) -> dict:  # noqa: ANN001
        return self.detalhes[int(execucao_id)]


def _lote(n8n: N8nFalso, ids: range, runs: dict, mode: str = "webhook") -> None:
    linhas = [{"id": str(i), "workflowId": "wf", "mode": mode, "startedAt": f"2026-10-19T12:00:{i % 60:02d}Z"}
              for i in ids]
    n8n.paginas = [{"data": sorted(linhas, key=lambda r: -int(r["id"]))}]
    n8n.detalhes.update({i: _execucao(runs) for i in ids})


class TestColetor:
    async def test_primeira_passada_so_aprende(self) -> None:
        n8n = N8nFalso()
        _lote(n8n, range(1, 31), {"Webhook": _rodou(1, 1)})
        resultado = await ExecutionPathCollector(n8n, InMemoryRepository()).run()
        assert resultado.extra["eventos"] == []

    async def test_etapa_do_caminho_normal_que_sumiu_vira_alerta_com_a_causa(self) -> None:
        n8n, repo = N8nFalso(), InMemoryRepository()
        coletor = ExecutionPathCollector(n8n, repo)
        _lote(n8n, range(1, 31), CAMINHO_NORMAL)
        await coletor.run()
        _lote(n8n, range(31, 32), {"Webhook": _rodou(1, 1), "IF": _rodou(2, 0, 1), "Avisa": _rodou(3, 1)})
        eventos = (await coletor.run()).extra["eventos"]
        assert [e.event_type for e in eventos] == ["node_not_executed"]
        assert eventos[0].detail["nos"] == ["Grava", "Responde"]
        assert "'IF' mandou os itens por outra saida" in eventos[0].detail["detalhe"]
        assert eventos[0].detail["caminho"].startswith("1. Webhook")

    async def test_erro_escondido_vira_alerta(self) -> None:
        n8n = N8nFalso()
        coletor = ExecutionPathCollector(n8n, InMemoryRepository())
        _lote(n8n, range(1, 3), CAMINHO_NORMAL)
        await coletor.run()
        _lote(n8n, range(3, 4), {**CAMINHO_NORMAL, "Grava": _rodou(3, 1, erro={"message": "deadlock"})})
        eventos = (await coletor.run()).extra["eventos"]
        assert [e.event_type for e in eventos] == ["node_error_hidden"]
        assert "deadlock" in eventos[0].detail["erro"]

    async def test_execucao_manual_e_ignorada(self) -> None:
        n8n = N8nFalso()
        coletor = ExecutionPathCollector(n8n, InMemoryRepository())
        _lote(n8n, range(1, 3), CAMINHO_NORMAL)
        await coletor.run()
        _lote(n8n, range(3, 4), {**CAMINHO_NORMAL, "Grava": _rodou(3, 1, erro={"message": "x"})}, mode="manual")
        assert (await coletor.run()).extra["eventos"] == []

    async def test_nao_rele_o_que_ja_leu(self) -> None:
        n8n = N8nFalso()
        coletor = ExecutionPathCollector(n8n, InMemoryRepository())
        _lote(n8n, range(1, 5), CAMINHO_NORMAL)
        await coletor.run()
        assert (await coletor.run()).collected == 0
