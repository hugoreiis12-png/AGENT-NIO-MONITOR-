"""Coleta de execucoes com n8n mockado (respx).

O caso que este arquivo existe para provar: cursor invalido NAO pode virar
silencio. Se o cursor morreu porque o n8n podou execucoes antigas, o intervalo
perdido precisa virar evento de buraco -- senao "todo historico" e mentira.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from agent_nio_monitor.sources.n8n.client import CursorInvalid, N8NApiError, N8NClient
from agent_nio_monitor.sources.n8n.collector import ExecutionErrorCollector
from agent_nio_monitor.store.repository import CursorState
from agent_nio_monitor.store.memory_repository import InMemoryRepository

BASE = "http://n8n.test"
FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "executions_erros_reais.json"
)


@pytest.fixture
def repo() -> InMemoryRepository:
    return InMemoryRepository()


def _pagina(items: list[dict], next_cursor: str | None = None) -> dict:
    return {"data": items, "nextCursor": next_cursor}


@pytest.fixture
def erros() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["data"]


class TestCliente:
    @respx.mock
    async def test_header_e_x_n8n_api_key(self) -> None:
        """`Authorization: Bearer` nao funciona no n8n. O doc original errava."""
        rota = respx.get(f"{BASE}/api/v1/executions").mock(
            return_value=httpx.Response(200, json=_pagina([]))
        )
        async with N8NClient(BASE, "chave-secreta") as c:
            await c._get("/api/v1/executions")
        assert rota.calls[0].request.headers["X-N8N-API-KEY"] == "chave-secreta"
        assert "Authorization" not in rota.calls[0].request.headers

    @respx.mock
    async def test_403_explica_permissao_em_vez_de_so_codigo(self) -> None:
        respx.get(f"{BASE}/api/v1/users").mock(
            return_value=httpx.Response(403, json={"message": "forbidden"})
        )
        async with N8NClient(BASE, "k") as c:
            with pytest.raises(N8NApiError) as ei:
                await c.list_users()
        assert "owner/admin" in str(ei.value), "erro precisa dizer o que fazer"

    @respx.mock
    async def test_404_vira_cursor_invalid(self) -> None:
        respx.get(f"{BASE}/api/v1/executions/999").mock(
            return_value=httpx.Response(404, json={"message": "not found"})
        )
        async with N8NClient(BASE, "k") as c:
            with pytest.raises(CursorInvalid):
                await c.get_execution(999)

    @respx.mock
    async def test_history_ausente_devolve_none_e_nao_levanta(self) -> None:
        """Rota inexistente nesta versao e cenario conhecido, nao erro fatal."""
        respx.get(f"{BASE}/api/v1/workflows/x/history").mock(
            return_value=httpx.Response(404, json={})
        )
        async with N8NClient(BASE, "k") as c:
            assert await c.workflow_history("x") is None


class TestColeta:
    @respx.mock
    async def test_coleta_deduplica_por_id(self, repo, erros) -> None:
        respx.get(f"{BASE}/api/v1/executions").mock(
            return_value=httpx.Response(200, json=_pagina(erros, None))
        )
        async with N8NClient(BASE, "k") as c:
            c1 = ExecutionErrorCollector(c, repo, fetch_payload=False)
            r1 = await c1.run()
            r2 = await c1.run()  # segunda rodada: nada novo
        assert r1.collected == 25
        assert r2.collected == 0, "dedup por id falhou"
        assert len(repo.executions) == 25

    @respx.mock
    async def test_cursor_e_persistido(self, repo, erros) -> None:
        respx.get(f"{BASE}/api/v1/executions").mock(
            return_value=httpx.Response(200, json=_pagina(erros, "CURVEDOR"))
        )
        async with N8NClient(BASE, "k") as c:
            await ExecutionErrorCollector(c, repo, fetch_payload=False).run()
        assert repo.get_cursor("n8n", "errors").cursor == "CURVEDOR"

    @respx.mock
    async def test_paginacao_percorre_todas_as_paginas(self, repo) -> None:
        """`max_pages` existe para nao ficar preso quando o nextCursor nao acaba."""
        def _pagina_por_chamada(_req):  # noqa: ANN001
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": 2, "status": "error", "startedAt": "2026-10-12T10:00:00Z",
                         "workflowId": "w"},
                        {"id": 1, "status": "error", "startedAt": "2026-10-12T09:00:00Z",
                         "workflowId": "w"},
                    ],
                    "nextCursor": "SEMPRE",
                },
            )

        respx.get(f"{BASE}/api/v1/executions").mock(side_effect=_pagina_por_chamada)
        async with N8NClient(BASE, "k", max_pages=3) as c:
            r = await ExecutionErrorCollector(c, repo, fetch_payload=False).run()
        assert r.collected == 6, "3 paginas x 2 itens, dedup por id"

    @respx.mock
    async def test_cursor_invalido_registra_buraco_e_reinicia(self, repo, erros) -> None:
        """O teste que justifica o desenho inteiro.

        Cursor morta = o n8n podou o que estava entre o ultimo ponto e agora.
        Os eventos desse intervalo sumiram. A resposta correta e: registrar o
        buraco, avisar, e varrer de novo -- nunca 'tentar a mesma cursor'.
        """
        repo.set_cursor(CursorState("n8n", "errors", cursor="CURSOR_MORTA"))
        chamadas = {"n": 0}

        def _handler(req):  # noqa: ANN001
            if req.url.params.get("cursor") == "CURSOR_MORTA":
                return httpx.Response(404, json={"message": "cursor not found"})
            chamadas["n"] += 1
            if chamadas["n"] == 1:
                return httpx.Response(200, json=_pagina(erros, "CURSOR_NOVA"))
            return httpx.Response(200, json=_pagina([], None))

        respx.get(f"{BASE}/api/v1/executions").mock(side_effect=_handler)
        async with N8NClient(BASE, "k") as c:
            r = await ExecutionErrorCollector(c, repo, fetch_payload=False).run()

        assert r.gap is True, "buraco nao foi detectado"
        run = repo.reconciliations[-1]
        assert run["gaps"] == 1
        assert run["cursor_valid"] is False, "buraco precisa ficar registrado, nao sumir"
        assert r.collected == 25, "apos o reset a varredura completa acontece"
        assert (
            repo.get_cursor("n8n", "errors").cursor == "CURSOR_NOVA"
        ), "o cursor morto precisa ser substituido, senao o proximo poll falha igual"

    @respx.mock
    async def test_cursor_invalido_por_400_tambem_e_buraco(self, repo, erros) -> None:
        """Algumas versoes respondem 400 em vez de 404. Se isso virar erro
        transitorio, o buraco nunca e reportado -- o pior resultado possivel."""
        repo.set_cursor(CursorState("n8n", "errors", cursor="CURSOR_MORTA"))

        def _handler(req):  # noqa: ANN001
            if req.url.params.get("cursor"):
                return httpx.Response(
                    400, json={"message": "Invalid pagination cursor supplied"}
                )
            return httpx.Response(200, json=_pagina(erros, None))

        respx.get(f"{BASE}/api/v1/executions").mock(side_effect=_handler)
        async with N8NClient(BASE, "k") as c:
            r = await ExecutionErrorCollector(c, repo, fetch_payload=False).run()
        assert r.gap is True
        assert r.collected == 25

    @respx.mock
    async def test_400_nao_relacionado_a_cursor_nao_vira_buraco(self, repo) -> None:
        """Nem todo 400 e cursor. Um filtro invalido e erro de config, e nao
        perda de dado -- tratá-lo como buraco seria alarme falso."""
        respx.get(f"{BASE}/api/v1/executions").mock(
            return_value=httpx.Response(400, json={"message": "status must be a string"})
        )
        async with N8NClient(BASE, "k") as c:
            r = await ExecutionErrorCollector(c, repo, fetch_payload=False).run()
        assert r.gap is False
        assert r.error is not None

    @respx.mock
    async def test_erro_de_reconciliacao_e_registrado(self, repo) -> None:
        """Toda rodada deixa rastro. Auditoria de 'o que o Hermes viu' depende disso."""
        respx.get(f"{BASE}/api/v1/executions").mock(
            return_value=httpx.Response(200, json=_pagina([]))
        )
        async with N8NClient(BASE, "k") as c:
            await ExecutionErrorCollector(c, repo, fetch_payload=False).run()
        assert repo.reconciliations[-1]["source"] == "n8n"
        assert repo.reconciliations[-1]["scope"] == "errors"

    @respx.mock
    async def test_payload_com_segredo_e_redigido_antes_do_repo(self, repo) -> None:
        """O caminho mais perigoso: payload de workflow com senha. O InMemory
        Repository redige na fronteira, e o Postgres faz o mesmo."""
        exec_id = 63090
        respx.get(f"{BASE}/api/v1/executions").mock(
            return_value=httpx.Response(
                200,
                json=_pagina(
                    [{"id": exec_id, "status": "error", "workflowId": "w",
                      "startedAt": "2026-10-12T16:05:28Z"}]
                ),
            )
        )
        respx.get(f"{BASE}/api/v1/executions/{exec_id}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": exec_id,
                    "error": {"message": "timeout"},
                    "data": {
                        "resultData": {
                            "lastNodeExecuted": "HTTP Request",
                            "runData": {
                                "HTTP Request": [
                                    {"data": {"parameters": {
                                        "name": "Password", "value": "jfc2025"}}}
                                ]
                            },
                        }
                    },
                },
            )
        )
        async with N8NClient(BASE, "k") as c:
            await ExecutionErrorCollector(c, repo, fetch_payload=True).run()

        falha = repo.failures[exec_id]
        assert falha.error_msg == "timeout"
        assert falha.last_node == "HTTP Request"
        assert "jfc2025" not in repr(falha.run_data), "segredo sobreviveu no payload"

    @respx.mock
    async def test_falha_no_payload_nao_perde_o_evento(self, repo) -> None:
        """O erro principal (execucao falhou) e mais importante que o detalhe."""
        respx.get(f"{BASE}/api/v1/executions").mock(
            return_value=httpx.Response(
                200,
                json=_pagina([{"id": 1, "status": "error", "workflowId": "w",
                               "startedAt": "2026-10-12T10:00:00Z"}]),
            )
        )
        respx.get(f"{BASE}/api/v1/executions/1").mock(return_value=httpx.Response(500))
        async with N8NClient(BASE, "k") as c:
            r = await ExecutionErrorCollector(c, repo, fetch_payload=True).run()
        assert r.collected == 1
        assert 1 in repo.executions


class TestStreak:
    async def test_streak_acumula(self, repo) -> None:
        base = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
        for i in range(5):
            s = repo.bump_streak("wf1", base, "proj", "Meu WF")
            assert s.count == i + 1
        assert s.first_at == base
        assert s.last_at == base

    async def test_streak_por_workflow_nao_conflui(self, repo) -> None:
        base = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
        repo.bump_streak("a", base, None, None)
        repo.bump_streak("b", base, None, None)
        repo.bump_streak("a", base, None, None)
        assert repo.get_streak("a").count == 2
        assert repo.get_streak("b").count == 1