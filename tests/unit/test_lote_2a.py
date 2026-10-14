"""Lote 2A: tres correcoes locais que antes nao tinham teste.

Nenhum dos tres itens abaixo falhava -- a suite ficava verde com o defeito
presente. Nenhum deles quebrava o sistema; os tres deixavam o agente mais
fragil do que precisava:

#11  O snapshot do diff era gravado ANTES do evento. Um crash entre os dois
     avanca o baseline e a mudanca nunca mais e detectada -- perda silenciosa.
#13  O coletor de execucoes (o mais critico do agente) chamava o PRIVADO
     `_iter_pages` do cliente, acorrentando-se ao detalhe interno de paginacao.
#17  `Agent` lia `policy.yaml` uma segunda vez, criando um segundo
     `PolicyConfig` para a mesma fonte de verdade.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import respx

from agent_nio_monitor.config.loader import load
from agent_nio_monitor.sources.n8n.workflow_monitor import WorkflowMonitor
from agent_nio_monitor.store.memory_repository import InMemoryRepository

RAIZ = Path(__file__).resolve().parents[2]
SRC = RAIZ / "src" / "agent_nio_monitor"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import agent_nio_monitor.config.loader as loader

    monkeypatch.setattr(loader, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("N8N_API_KEY", "k")
    monkeypatch.setenv("DATABASE_DSN", "postgresql://u:s@localhost/h")


def _workflow(wid: str = "wf1", **extra) -> dict:
    base = {
        "id": wid,
        "name": "Processa Checkout",
        "active": True,
        "versionId": "v1",
        "nodes": [{"id": "n1", "name": "Set", "type": "n8n-nodes-base.set", "typeVersion": 3}],
        "connections": {},
        "activeVersion": {"authors": "Ana <ana@x.com>"},
    }
    base.update(extra)
    return base


def _workflow_antigo(wid: str = "wf1") -> dict:
    """Estado de ANTES: um node a mais, entao o diff nao e vazio.

    Precisa diferir do estado atual: com corpos iguais, `diff_workflows` devolve
    vazio e o monitor retorna antes de qualquer evento -- o teste passaria sem
    exercitar nada.
    """
    return _workflow(
        wid,
        versionId="v0",
        nodes=[
            {"id": "n1", "name": "Set", "type": "n8n-nodes-base.set", "typeVersion": 3},
            {"id": "n2", "name": "HTTP", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4},
        ],
    )


class _ClienteFalso:
    """Entrega uma lista fixa de workflows e registra as chamadas de API."""

    def __init__(self, workflows: list[dict]) -> None:
        self._workflows = workflows

    async def iter_workflows(self, **_kw):
        for wf in self._workflows:
            yield wf


class _RepoEspiao(InMemoryRepository):
    """Grava a ORDEM das escritas. E o que permite travar a invariante do #11
    sem depender de onde uma falha acontece."""

    def __init__(self) -> None:
        super().__init__()
        self.ordem: list[str] = []

    def save_workflow_snapshot(self, workflow_id, version_id, active, body) -> None:
        self.ordem.append("snapshot")
        super().save_workflow_snapshot(workflow_id, version_id, active, body)

    def upsert_resource(self, **kw) -> None:
        self.ordem.append("resource")
        super().upsert_resource(**kw)


class TestNumero11SnapshotNoFim:
    async def test_snapshot_e_a_ultima_escrita_do_ciclo(self) -> None:
        """Invariante direta: o baseline do diff so pode avancar DEPOIS que os
        eventos existem. Com o codigo antigo, `snapshot` era a primeira
        escrita e esta assercao falhava."""
        repo = _RepoEspiao()
        repo.save_workflow_snapshot("wf1", "v0", True, _workflow_antigo())  # baseline anterior
        repo.ordem.clear()

        monitor = WorkflowMonitor(_ClienteFalso([_workflow()]), repo)
        resultado = await monitor.check_structural_diff()

        assert repo.ordem == ["resource", "snapshot"], repo.ordem
        assert resultado.extra["eventos"], "a mudanca de `v0`->`v1` tem que gerar evento"

    async def test_evento_que_falha_nao_avanca_o_baseline(self) -> None:
        """O crash que a ordem evita.

        Se o evento falha DEPOIS do snapshot, o proximo ciclo parte do estado
        novo, a mudanca nao aparece no diff, e ninguem e avisado. Com o
        snapshot por ultimo, o baseline fica no estado antigo e a mudanca e
        redetectada no proximo ciclo.
        """
        repo = _RepoEspiao()
        repo.save_workflow_snapshot("wf1", "v0", True, _workflow_antigo())
        repo.ordem.clear()

        monitor = WorkflowMonitor(_ClienteFalso([_workflow()]), repo)
        estourou = RuntimeError("falha ao montar o evento")
        monkey = pytest.MonkeyPatch()
        monkey.setattr(monitor, "_evento_alterado", _explode(estourou))
        try:
            with pytest.raises(RuntimeError):
                await monitor.check_structural_diff()
        finally:
            monkey.undo()

        assert repo.ordem == ["resource"], f"o baseline avancou: {repo.ordem}"
        assert len(repo.snapshots["wf1"]) == 1, "so o baseline anterior pode existir"

    async def test_mudanca_redetectada_no_ciclo_seguinte(self) -> None:
        """Fecha o ciclo: depois da falha, o proximo ciclo tem que ver de novo."""
        repo = _RepoEspiao()
        repo.save_workflow_snapshot("wf1", "v0", True, _workflow_antigo())

        monitor = WorkflowMonitor(_ClienteFalso([_workflow()]), repo)
        monkey = pytest.MonkeyPatch()
        monkey.setattr(monitor, "_evento_alterado", _explode(RuntimeError("x")))
        try:
            with pytest.raises(RuntimeError):
                await monitor.check_structural_diff()
        finally:
            monkey.undo()

        resultado = await WorkflowMonitor(_ClienteFalso([_workflow()]), repo).check_structural_diff()
        assert [e.event_type for e in resultado.extra["eventos"]] == ["workflow_changed"], (
            "a mudanca tem que reaparecer -- e o sinal de que o baseline nao "
            "avancou por cima dela"
        )

    async def test_workflow_novo_tambem_grava_snapshot_por_ultimo(self) -> None:
        repo = _RepoEspiao()
        repo.save_workflow_snapshot("outro", "v0", True, _workflow_antigo("outro"))

        monitor = WorkflowMonitor(_ClienteFalso([_workflow("novo")]), repo)
        await monitor.check_structural_diff()

        assert repo.ordem[-1] == "snapshot", repo.ordem


def _explode(exc: Exception):
    def _f(*_a, **_kw):  # noqa: ANN202
        raise exc

    return _f


class TestNumero13SemAcessoPrivado:
    """O coletor de execucoes e o mais critico do agente. Ele nao pode depender
    de detalhe interno de outro modulo."""

    def _chamadas_ao_metodo_privado(self) -> list[str]:
        """Varre `sources/` por acesso a `._iter_pages` / `._get` de cliente."""
        achados: list[str] = []
        for caminho in (SRC / "sources").rglob("*.py"):
            if "__pycache__" in caminho.parts or caminho.name == "client.py":
                continue  # `client.py` DEFINE o privado; o problema e o uso
            arvore = ast.parse(caminho.read_text(encoding="utf-8"))
            for no in ast.walk(arvore):
                if (
                    isinstance(no, ast.Attribute)
                    and no.attr.startswith("_")
                    and isinstance(no.value, ast.Attribute)
                    and no.value.attr in ("client", "self")
                ):
                    achados.append(f"{caminho.relative_to(SRC)}:{no.lineno} -> {no.attr}")
        return achados

    def test_coletor_nao_chama_metodo_privado_do_cliente(self) -> None:
        achados = self._chamadas_ao_metodo_privado()
        assert not achados, (
            f"uso de metodo privado do cliente em {achados}. Qualquer refatoracao "
            "de paginacao quebraria o coletor em silencio."
        )

    @respx.mock
    async def test_metodo_publico_expoe_o_next_cursor(self) -> None:
        """`iter_executions()` achata a pagina e perde o `nextCursor`. O cursor
        de retomada depende dele, entao a forma paginada precisa ser publica."""
        import httpx

        from agent_nio_monitor.sources.n8n.client import N8NClient

        base = "http://n8n.test"
        paginas = [
            {"data": [{"id": 1}], "nextCursor": "CURSOR_NOVA"},
            {"data": [{"id": 2}], "nextCursor": None},
        ]
        respx.get(f"{base}/api/v1/executions").mock(
            side_effect=[httpx.Response(200, json=p) for p in paginas]
        )
        async with N8NClient(base, "k") as client:
            recebidas = [p async for p in client.iter_executions_pages()]

        assert [p["nextCursor"] for p in recebidas] == ["CURSOR_NOVA", None], (
            "sem o nextCursor o coletor nao consegue gravar o cursor de retomada"
        )
        assert recebidas[0]["data"][0]["id"] == 1

    @respx.mock
    async def test_cursor_que_nao_termina_trunca_em_silencio(self) -> None:
        """Defeito caracterizado, NAO corrigido -- e fora do lote 2A.

        `_iter_pages` sai do laco em `max_pages` SEM levantar e SEM sinalizar.
        Um `nextCursor` que o servidor devolve sempre leva a 50 paginas e a
        nenhuma: o gerador termina "com sucesso", e o coletor avanca o cursor
        para `nextCursor` sem ter visto o resto.

        Este teste existe para deixar o comportamento medido e nomeado. Quando
        for corrigido (limite sinalizado, ou cursor repetido detectado), ele
        deve falhar -- e a falha e o sinal de que a correcao entrou.
        """
        import httpx

        from agent_nio_monitor.sources.n8n.client import N8NClient

        base = "http://n8n.test"
        respx.get(f"{base}/api/v1/executions").mock(
            return_value=httpx.Response(
                200, json={"data": [{"id": 1}], "nextCursor": "SEM_FIM"}
            )
        )
        async with N8NClient(base, "k", max_pages=5) as client:
            recebidas = [p async for p in client.iter_executions_pages()]

        assert len(recebidas) == 5, (
            "hoje trunca em silencio. Se este teste falhar com outra contagem, "
            "`_iter_pages` passou a sinalizar o limite -- atualize a decisao."
        )

    @respx.mock
    async def test_achatamento_continua_fornecendo_os_itens(self) -> None:
        import httpx
        import respx

        from agent_nio_monitor.sources.n8n.client import N8NClient

        base = "http://n8n.test"
        respx.get(f"{base}/api/v1/executions").mock(
            return_value=httpx.Response(
                200, json={"data": [{"id": 1}, {"id": 2}], "nextCursor": None}
            )
        )
        async with N8NClient(base, "k") as client:
            itens = [i async for i in client.iter_executions()]

        assert [i["id"] for i in itens] == [1, 2], (
            "o metodo achatado nao pode regredir ao refatorar o paginado"
        )


class TestNumero17UmaSoFonteDeVerdade:
    async def test_agent_usa_a_policy_da_config(self) -> None:
        from agent_nio_monitor.agent import Agent

        cfg = load()
        agent = Agent(cfg, InMemoryRepository())
        try:
            assert agent.policy is cfg.policy, (
                "dois objetos PolicyConfig para o mesmo arquivo divergem "
                "silenciosamente se o arquivo mudar no meio do boot"
            )
        finally:
            await agent.llm.aclose()
            await agent.notifier.aclose()
            await agent.n8n.aclose()

    def test_agent_nao_chama_load_policy(self) -> None:
        """Trava a fiaao. `load_policy()` ainda existe -- e usada por teste --
        mas o `Agent` nao pode mais chamar: a leitura extra era o defeito."""
        arvore = ast.parse((SRC / "agent.py").read_text(encoding="utf-8"))
        chamadas = {
            no.func.id
            for no in ast.walk(arvore)
            if isinstance(no, ast.Call) and isinstance(no.func, ast.Name)
        }
        assert "load_policy" not in chamadas, "load_policy nao deve ser chamada pelo Agent"

    async def test_as_duas_fontes_concordam(self) -> None:
        """Sanidade: a policy do Agent tem que ser a mesma que o loader leria."""
        from agent_nio_monitor.agent import Agent
        from agent_nio_monitor.config.loader import load_policy

        cfg = load()
        esperado = load_policy()
        agent = Agent(cfg, InMemoryRepository())
        try:
            assert len(agent.policy.rules) == len(esperado.rules)
            assert agent.policy.max_recipients == esperado.max_recipients
        finally:
            await agent.llm.aclose()
            await agent.notifier.aclose()
            await agent.n8n.aclose()