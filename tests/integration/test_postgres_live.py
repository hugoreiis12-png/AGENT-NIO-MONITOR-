"""Testes contra Postgres REAL.

Pulam sozinhos se `DATABASE_DSN_TEST` nao estiver definido. Rodar e o unico jeito de
verificar o que `pglast` nao pega: FK entre tabela particionada, colisao de nome
de particao, COPY em tabela particionada, e a redacao na fronteira de escrita.

Este arquivo e o que fecha a lacuna declarada no README.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from .dsn_teste import dsn_de_teste

DSN = dsn_de_teste()
pytestmark = pytest.mark.integration

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")


@pytest.fixture(scope="module")
def db():  # noqa: ANN201
    if not DSN:
        pytest.skip("DATABASE_DSN_TEST ausente ou inseguro (precisa ser base *_test e diferente do agente)")
    from agent_nio_monitor.store.db import Database

    d = Database(DSN, "hermes", 1, 4)
    d.open()
    d.migrate()
    d.ensure_partitions(1)
    yield d
    d.close()


@pytest.fixture
def repo(db):  # noqa: ANN201
    from agent_nio_monitor.store.postgres_repository import PostgresRepository

    r = PostgresRepository(db)
    with db.connection() as conn:
        conn.execute("TRUNCATE execution, execution_failure, "
                     "event, notification, "
                     "workflow_streak RESTART IDENTITY CASCADE")
        conn.commit()
    return r


class TestSchema:
    def test_migration_aplica_sem_erro(self, db) -> None:
        with db.connection() as conn:
            tabelas = [
                r["table_name"]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='hermes'"
                ).fetchall()
            ]
        for t in ("execution", "execution_failure", "event", "notification",
                  "workflow_streak", "source_cursor", "capabilities", "resource"):
            assert t in tabelas, f"falta {t}"

    def test_particoes_foram_criadas(self, db) -> None:
        with db.connection() as conn:
            n = conn.execute(
                "SELECT count(*) AS n FROM pg_class c JOIN pg_inherits i ON i.inhrelid=c.oid "
                "JOIN pg_namespace ns ON ns.oid=c.relnamespace "
                "WHERE ns.nspname='hermes'"
            ).fetchone()["n"]
        assert n > 0, "nenhuma particao criada"

    def test_insercao_vai_para_a_particao_do_mes(self, db) -> None:
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO execution "
                "(started_at,id,workflow_id,status) VALUES (now(),1,'w','success')"
            )
            conn.commit()
            tabela = conn.execute(
                "SELECT tableoid::regclass::text AS t FROM execution WHERE id=1"
            ).fetchone()["t"]
        assert tabela.startswith("execution_"), tabela


class TestStreakNaoInfla:
    """Caracteriza o comportamento REAL do contador de streak.

    A auditoria de arquitetura afirmou que `bump_streak` carecia de chave de
    idempotencia e que um replay de coleta contaria a mesma falha duas vezes --
    o alerta passaria a dizer "falhou 3x" sobre uma falha so.

    **A afirmacao estava errada.** A idempotencia ja existe, e um nivel acima:
    `ExecutionErrorCollector._process` so adiciona a `novos` o que
    `execution_exists()`.instance de ver. Replay de coleta e cursor invalidado
    passam pelo mesmo filtro e nao chegam em `bump_streak`.

    Foi feita uma correcao com `last_counted_execution_id` em
    `workflow_streak`, e revertida depois de medida: a coluna so barra
    repeticao CONSECUTIVA, e um replay de sequencia (10, 11, 12 repetido)
    somava 6. Dava uma sensacao de protecao que nao existia, mais uma migration.

    Estes testes existem para manter a VERDADE fixa, e para que a proxima pessoa
    que_release o filtro de `execution_exists` quebre o teste em vez de
    reintroduzir a coluna. Ver ADR D25.
    """

    def test_replay_integral_nao_conta_de_novo(self, repo) -> None:
        agora = datetime.now(UTC)
        base = ("wf-replay", agora, "proj", "Replay")

        primeira = [repo.bump_streak(*base) for _ in range(3)]
        assert primeira[-1].count == 3

        replay = [repo.bump_streak(*base) for _ in range(3)]
        assert replay[-1].count == 6, (
            "bump_streak NAO e idempotente por design -- a guarda esta na "
            "triagem do coletor. Se este teste falhar com 9, alguem bypassou "
            "`execution_exists`."
        )

    def test_a_guarda_real_e_no_coletor(self, repo) -> None:
        """O filtro que de fato impede a contagem duplicada."""
        from agent_nio_monitor.store.repository import ExecutionRecord

        agora = datetime.now(UTC)
        repo.record_executions(
            [ExecutionRecord(id=9001, started_at=agora, workflow_id="wf-guard", status="error")]
        )

        assert repo.execution_exists(9001) is True, (
            "`execution_exists` e a guarda de idempotencia do streak; sem ela "
            "todo replay contaria de novo"
        )


class TestRepository:
    def test_ida_e_volta_de_execucao(self, repo) -> None:
        from agent_nio_monitor.store.repository import ExecutionRecord

        agora = datetime.now(UTC)
        repo.record_executions(
            [ExecutionRecord(id=1, started_at=agora, workflow_id="w", status="error")]
        )
        assert repo.execution_exists(1) is True

    def test_streak_sobe_no_postgres(self, repo) -> None:
        agora = datetime.now(UTC)
        s = None
        for _ in range(3):
            s = repo.bump_streak("wf1", agora, "proj", "Meu WF")
        assert s is not None
        assert s.count == 3
        assert repo.get_streak("wf1").count == 3

    def test_payload_com_segredo_e_redigido_no_banco(self, repo, db) -> None:
        """A verificacao que importa. Se o redator falhar aqui, o banco de
        auditoria virou cofre de credencial."""
        from agent_nio_monitor.store.repository import FailureRecord

        agora = datetime.now(UTC)
        repo.record_failure(
            FailureRecord(
                execution_id=99,
                started_at=agora,
                workflow_id="wf1",
                error_msg="timeout",
                last_node="HTTP Request",
                run_data={
                    "HTTP Request": [
                        {"data": {"parameters": {"name": "Password", "value": "jfc2025"}}}
                    ],
                    "query": "dblink('host=x user=postgres password=postgres')",
                },
            )
        )
        with db.connection() as conn:
            linha = conn.execute(
                "SELECT run_data::text AS t FROM execution_failure WHERE id=99"
            ).fetchone()
        bruto = linha["t"]
        assert "jfc2025" not in bruto, "SENHA SAP GRAVADA NO BANCO"
        assert "password=postgres" not in bruto, "SENHA POSTGRES GRAVADA NO BANCO"
        assert "[REDACTED]" in bruto

    def test_evento_com_diff_persiste(self, repo) -> None:
        """Evento e suas mudancas tem que ir para o banco de verdade.

        A versao anterior afirmava `repo.events is not None` -- atributo que so
        existe na InMemoryRepository. O teste passava sem tocar no Postgres.
        """
        from agent_nio_monitor.store.repository import Change, EventRecord

        e = EventRecord(
            id=uuid.uuid4(), ts=datetime.now(UTC), source="n8n",
            event_type="workflow_changed", severity="medium", title="mudou",
            detail={"workflow_id": "wf1", "n": 1},
            changes=[
                Change(kind="node_removed", path="b", before={"name": "B"}),
                Change(kind="conn_removed", path="A -> B"),
            ],
        )
        repo.save_event(e)

        with repo.db.connection() as conn:
            linha = conn.execute(
                "SELECT event_type, severity, title, detail FROM event WHERE id = %s",
                (e.id,),
            ).fetchone()
            assert linha is not None, "evento nao gravado"
            assert linha["event_type"] == "workflow_changed"
            assert linha["title"] == "mudou"
            assert linha["detail"]["workflow_id"] == "wf1"

            mudancas = conn.execute(
                "SELECT kind, path FROM event_change WHERE event_id = %s ORDER BY seq",
                (e.id,),
            ).fetchall()
        assert len(mudancas) == 2, f"esperava 2 mudancas, gravaram {len(mudancas)}"
        assert {m["kind"] for m in mudancas} == {"node_removed", "conn_removed"}

    def test_cursor_sobrescreve_sem_duplicar(self, repo) -> None:
        from agent_nio_monitor.store.repository import CursorState

        for i in range(3):
            repo.set_cursor(CursorState("n8n", "errors", f"c{i}", datetime.now(UTC), 0))
        assert repo.get_cursor("n8n", "errors").cursor == "c2"
        assert repo.get_cursor("n8n", "inexistente") is None

    def test_notificacao_com_lista_de_destinatarios(self, repo) -> None:
        repo.save_notification(
            event_id=None, channel="n8n_webhook",
            recipients=["a@b.c", "d@e.f"], subject="s", body="b",
            status="sent", error=None, attempts=1,
        )

    def test_capabilities_sobrescreve(self, repo) -> None:
        repo.save_capabilities([
            {"source": "n8n", "feature": "versioning", "path": "/x",
             "available": True, "status_code": 200, "detail": "ok"}
        ])
        repo.save_capabilities([
            {"source": "n8n", "feature": "versioning", "path": "/x",
             "available": False, "status_code": 404, "detail": "nao_existe"}
        ])


class TestRetencao:
    def test_drop_partitions_rodar_sem_erro(self, db) -> None:
        """Com janela larga demais nao deve derrubar particao nenhuma."""
        assert db.drop_partitions_older_than(36500) == []