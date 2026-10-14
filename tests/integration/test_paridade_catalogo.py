"""Paridade InMemory x Postgres nas escritas de catalogo.

Snapshot de workflow, recurso (credencial/stack), capacidade, reconciliacao e
observacao de container. Os coletores so leem os campos comparados aqui; se o
Postgres devolver outra coisa, o diff e as transicoes de container mentem em
producao enquanto a suite em memoria passa.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_nio_monitor.redaction import REDACTED
from agent_nio_monitor.store.memory_repository import InMemoryRepository

from .dsn_teste import dsn_de_teste

DSN = dsn_de_teste()
pytestmark = pytest.mark.integration

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

TABELAS = "workflow_snapshot, resource, capabilities, reconciliation_run, container_observation"
# Mes corrente: `ensure_partitions(1)` so cria a particao dele.
AGORA = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture(scope="module")
def pg_repo():
    if not DSN:
        pytest.skip("DATABASE_DSN_TEST ausente ou inseguro (precisa ser base *_test e diferente do agente)")
    from agent_nio_monitor.store.db import Database
    from agent_nio_monitor.store.postgres_repository import PostgresRepository

    db = Database(DSN, "hermes", 1, 4)
    db.open()
    db.migrate()
    db.ensure_partitions(1)
    yield PostgresRepository(db)
    db.close()


@pytest.fixture
def repos(pg_repo):
    with pg_repo.db.connection() as conn:
        conn.execute(f"TRUNCATE {TABELAS} RESTART IDENTITY CASCADE")
        conn.commit()
    return [InMemoryRepository(), pg_repo]


class TestSnapshot:
    def test_ultimo_snapshot_e_o_mais_recente_e_redigido(self, repos) -> None:
        for repo in repos:
            assert repo.has_workflow_snapshots() is False
            assert repo.last_workflow_snapshot("wf") is None
            repo.save_workflow_snapshot("wf", "v1", True, {"nodes": []})
            repo.save_workflow_snapshot("wf", "v2", False, {"password": "jfc2025"})

        for repo in repos:
            ultimo = repo.last_workflow_snapshot("wf")
            assert repo.has_workflow_snapshots() is True
            assert (ultimo["version_id"], ultimo["active"]) == ("v2", False), type(repo).__name__
            assert ultimo["body"]["password"] == REDACTED, "segredo chegou ao banco"


class TestRecurso:
    def test_upsert_atualiza_em_vez_de_duplicar(self, repos) -> None:
        for repo in repos:
            repo.upsert_resource(source="portainer", kind="stack", external_id="7", name="erp", meta={"v": 1})
            repo.upsert_resource(source="portainer", kind="stack", external_id="7", name="erp", meta={"v": 2})
            repo.upsert_resource(source="n8n", kind="credential", external_id="c1", name="sap")

        memoria, postgres = ([(r["external_id"], r["name"], r.get("meta")) for r in repo.list_resources(
            "portainer", "stack")] for repo in repos)
        assert memoria == postgres == [("7", "erp", {"v": 2})]


class TestCapacidade:
    def test_sondar_de_novo_atualiza_a_linha(self, repos) -> None:
        pg = repos[1]
        linha = {"source": "n8n", "feature": "health", "path": "/healthz", "available": False, "status_code": 503}
        pg.save_capabilities([linha])
        pg.save_capabilities([{**linha, "available": True, "status_code": 200}])
        with pg.db.connection() as conn:
            linhas = conn.execute("SELECT available, status_code FROM capabilities").fetchall()
        assert [(r["available"], r["status_code"]) for r in linhas] == [(True, 200)]


class TestReconciliacao:
    def test_buraco_fica_registrado(self, repos) -> None:
        pg = repos[1]
        pg.record_reconciliation(source="n8n", scope="executions", expected=5, seen=2, gaps=3,
                                 cursor_valid=False, detail={"motivo": "prune"})
        with pg.db.connection() as conn:
            r = conn.execute("SELECT expected, seen, gaps, cursor_valid, detail FROM reconciliation_run").fetchone()
        assert (r["expected"], r["seen"], r["gaps"], r["cursor_valid"]) == (5, 2, 3, False)
        assert r["detail"] == {"motivo": "prune"}


class TestObservacaoDeContainer:
    def test_ultima_observacao_e_a_mais_recente(self, repos) -> None:
        for repo in repos:
            assert repo.last_container_observation("c1") is None
            repo.record_container_observation("c1", AGORA - timedelta(seconds=30), state="running",
                                              restart_count=0, mem_pct=10.0)
            repo.record_container_observation("c1", AGORA, state="exited", health="unhealthy",
                                              restart_count=3, mem_pct=95.5, cpu_pct=1.5)

        campos = ("state", "health", "restart_count", "mem_pct", "cpu_pct")
        memoria, postgres = ({c: repo.last_container_observation("c1")[c] for c in campos} for repo in repos)
        assert memoria == postgres == {"state": "exited", "health": "unhealthy", "restart_count": 3,
                                       "mem_pct": 95.5, "cpu_pct": 1.5}
