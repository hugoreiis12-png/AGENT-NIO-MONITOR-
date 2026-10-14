"""Contrato de paridade: InMemory e Postgres precisam concordar.

O bug que motivou este arquivo
------------------------------
`InMemoryRepository.bump_streak` zerava o contador quando `at > s.last_at`. O
Postgres ja fazia `count = count + 1` incondicional. Os testes rodavam SO
contra a InMemory, entao a divergencia era invisivel -- e o alerta mais
valioso do sistema (severity critical em `min_streak: 3`) ficava morto.

Enquanto este teste nao rodar, mock e producao podem divergir em silencio de
novo. E por isso que ele compara as DUAS implementacoes com a MESMA entrada, em
vez de afirmar a semantica duas vezes em lugares diferentes.

Pula sem `DATABASE_DSN_TEST`. Sem isso ele nao roda -- e nao finge que rodou.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from .dsn_teste import dsn_de_teste

from agent_nio_monitor.store.memory_repository import InMemoryRepository
from agent_nio_monitor.store.repository import (
    ExecutionRecord,
    FailureRecord,
)

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

DSN = dsn_de_teste()
BASE = datetime(2026, 10, 12, 16, 30, tzinfo=UTC)


@pytest.fixture(scope="module")
def pg_repo():
    """Postgres de teste. Apaga TUDO antes de comecar -- `hermes_test` e
    descartavel por definicao, nunca `hermes`."""
    if not DSN:
        pytest.skip("DATABASE_DSN_TEST ausente ou inseguro (precisa ser base *_test e diferente do agente)")
    from agent_nio_monitor.store.db import Database
    from agent_nio_monitor.store.postgres_repository import PostgresRepository

    db = Database(DSN, "hermes", 1, 4)
    db.open()
    db.migrate()
    db.ensure_partitions(1)
    repo = PostgresRepository(db)
    yield repo
    db.close()


@pytest.fixture
def limpo(pg_repo):
    with pg_repo.db.connection() as conn:
        conn.execute(
            "TRUNCATE execution, execution_failure, workflow_streak, event, "
            "notification RESTART IDENTITY CASCADE"
        )
        conn.commit()
    return pg_repo


# Sem timestamp comecando em 2026 e particionamento por mes -- as particoes
# criadas por `ensure_partitions(1)` cobrem o mes corrente, nao 2026. Por isso os
# cenarios usam timestamps dentro da janela ja particionada.
JANELA = datetime.now(UTC).replace(day=1, hour=12, minute=0, second=0, microsecond=0)


def _no_janela(minutos: float) -> datetime:
    return JANELA + timedelta(minutes=minutos)


@pytest.mark.parametrize(
    "ordem",
    ["crescente", "decrescente", "igual", "embaralhado"],
)
def test_streak_identico_nos_dois(limpo, ordem: str) -> None:
    """Mesma entrada, mesmo resultado. Sem isto, o mock mente."""
    if ordem == "crescente":
        ts = [_no_janela(37 * i) for i in range(9)]
    elif ordem == "decrescente":
        ts = [_no_janela(37 * (8 - i)) for i in range(9)]
    elif ordem == "igual":
        ts = [_no_janela(0)] * 9
    else:
        base = [_no_janela(37 * i) for i in range(9)]
        ts = base[::2] + base[1::2][::-1]

    memoria = InMemoryRepository()
    for t in ts:
        memoria.bump_streak("wf1", t, "proj1", "Processa Checkout")
    for t in ts:
        limpo.bump_streak("wf1", t, "proj1", "Processa Checkout")

    a, b = memoria.get_streak("wf1"), limpo.get_streak("wf1")
    assert b is not None
    assert (a.count, a.first_at, a.last_at) == (b.count, b.first_at, b.last_at), (
        f"divergencia na ordem '{ordem}': "
        f"memoria=({a.count}, {a.first_at}, {a.last_at}) "
        f"postgres=({b.count}, {b.first_at}, {b.last_at})"
    )
    assert a.count == 9
    assert a.workflow_name == b.workflow_name == "Processa Checkout"
    assert a.tenant_id == b.tenant_id == "proj1"


def test_streak_nome_preservado_apos_get(limpo) -> None:
    """`get_streak` precisa devolver tenant_id e workflow_name nos dois.

    Antes o Postgres devolvia um `Streak` sem esses campos (nao estavam no
    SELECT) e o nome sumia em producao.
    """
    memoria = InMemoryRepository()
    memoria.bump_streak("wf", _no_janela(0), "proj-xyz", "Meu Workflow")
    limpo.bump_streak("wf", _no_janela(0), "proj-xyz", "Meu Workflow")

    a, b = memoria.get_streak("wf"), limpo.get_streak("wf")
    assert a.workflow_name == b.workflow_name == "Meu Workflow"
    assert a.tenant_id == b.tenant_id == "proj-xyz"


def test_streak_reinsert_de_name_nao_apaga(limpo) -> None:
    memoria, limpo = InMemoryRepository(), limpo
    for repo in (memoria, limpo):
        repo.bump_streak("wf", _no_janela(0), "p", "Nome Original")
        repo.bump_streak("wf", _no_janela(1), None, None)
        s = repo.get_streak("wf")
        assert s.workflow_name == "Nome Original", (
            "COALESCE no Postgres e `or` na InMemory devem preservar o nome antigo"
        )


class TestInsercaoEmLote:
    def test_lote_insere_e_retorna_rowcount(self, limpo) -> None:
        lote = [
            ExecutionRecord(
                id=1000 + i, started_at=_no_janela(i), workflow_id=f"wf{i}",
                status="error", stopped_at=_no_janela(i),
            )
            for i in range(5)
        ]
        assert limpo.record_executions(lote) == 5

    def test_reinsert_nao_quebra_nem_duplica(self, limpo) -> None:
        """O bug do `COPY`: sem ON CONFLICT, reprocessar a mesma pagina (o que
        acontece depois de um reset de cursor) derrubava a transacao."""
        lote = [
            ExecutionRecord(id=2000 + i, started_at=_no_janela(i),
                            workflow_id="w", status="error")
            for i in range(5)
        ]
        assert limpo.record_executions(lote) == 5
        assert limpo.record_executions(lote) == 0, (
            "a segunda insercao deve ser ignorada, nao duplicar nem estourar"
        )
        with limpo.db.connection() as conn:
            total = conn.execute("SELECT count(*) AS n FROM execution").fetchone()["n"]
        assert total == 5

    def test_mesmo_id_data_diferente_coexistem(self, limpo) -> None:
        """PK e (id, started_at): execucoes diferentes com id repetido sao
        linhas diferentes."""
        limpo.record_executions([
            ExecutionRecord(id=3000, started_at=_no_janela(0), workflow_id="w",
                            status="error")
        ])
        limpo.record_executions([
            ExecutionRecord(id=3000, started_at=_no_janela(60), workflow_id="w",
                            status="error")
        ])
        with limpo.db.connection() as conn:
            total = conn.execute("SELECT count(*) AS n FROM execution").fetchone()["n"]
        assert total == 2

    def test_campos_none_viram_null(self, limpo) -> None:
        """tenant_id/name/mode null: o unnest precisa adaptar ARRAY com NULL."""
        limpo.record_executions([
            ExecutionRecord(id=4000, started_at=_no_janela(0), workflow_id="w",
                            status="running")
        ])
        with limpo.db.connection() as conn:
            linha = conn.execute(
                "SELECT tenant_id, workflow_name, mode, stopped_at, duration_ms "
                "FROM execution WHERE id=4000"
            ).fetchone()
        assert linha["tenant_id"] is None
        assert linha["stopped_at"] is None
        assert linha["duration_ms"] is None

    def test_lista_vazia_nao_chama_sql(self, limpo) -> None:
        assert limpo.record_executions([]) == 0


class TestInvariantesDoBanco:
    def test_check_rejeita_duracao_negativa(self, limpo) -> None:
        """A migration 002 precisa valer. Python faz clamp, o banco recusa."""
        from psycopg import errors as pgerrors

        with pytest.raises(pgerrors.CheckViolation), limpo.db.connection() as conn:
            conn.execute(
                "INSERT INTO execution (started_at, id, workflow_id, status, duration_ms) "
                "VALUES (%s, %s, %s, %s, %s)",
                (_no_janela(0), 9001, "w", "error", -1),
            )
            conn.commit()

    def test_check_rejeita_severidade_desconhecida(self, limpo) -> None:
        from uuid import uuid4

        from psycopg import errors as pgerrors

        with pytest.raises(pgerrors.CheckViolation), limpo.db.connection() as conn:
            conn.execute(
                "INSERT INTO event (ts, id, source, event_type, severity, title) "
                "VALUES (now(), %s, 'n8n', 'execution_failed', 'catastrofico', 'x')",
                (uuid4(),),
            )
            conn.commit()

    def test_check_rejeita_titulo_vazio(self, limpo) -> None:
        from uuid import uuid4

        from psycopg import errors as pgerrors

        with pytest.raises(pgerrors.CheckViolation), limpo.db.connection() as conn:
            conn.execute(
                "INSERT INTO event (ts, id, source, event_type, severity, title) "
                "VALUES (now(), %s, 'n8n', 'execution_failed', 'low', '   ')",
                (uuid4(),),
            )
            conn.commit()

    def test_payload_redigido_esta_no_banco(self, limpo) -> None:
        """A prova que so o banco real da: segredo nao sobrevive ao round-trip."""
        limpo.record_failure(
            FailureRecord(
                execution_id=5000, started_at=_no_janela(0), workflow_id="w",
                error_msg="timeout", last_node="HTTP Request",
                run_data={
                    "HTTP Request": [
                        {"data": {"parameters": {
                            "name": "Password", "value": "jfc2025"}}}
                    ],
                    "q": "dblink('host=x user=postgres password=postgres')",
                    "senha": "outraSenha123",
                },
            )
        )
        with limpo.db.connection() as conn:
            bruto = conn.execute(
                "SELECT run_data::text AS t FROM execution_failure WHERE id=5000"
            ).fetchone()["t"]
        for segredo in ("jfc2025", "password=postgres", "outraSenha123"):
            assert segredo not in bruto, f"'{segredo}' sobreviveu no banco"
        assert "[REDACTED]" in bruto

    def test_migration_nao_roda_duas_vezes(self, pg_repo) -> None:
        """`schema_migrations` precisa impedir reaplicacao."""
        segunda = pg_repo.db.migrate()
        assert segunda == [], f"reaplicou {segunda}"
        with pg_repo.db.connection() as conn:
            linhas = conn.execute("SELECT version FROM schema_migrations").fetchall()
        assert len(linhas) >= 2, "001 e 002 deveriam estar registradas"

    def test_execucao_vai_para_a_particao_do_mes(self, limpo) -> None:
        """O particionamento precisa estar ativo: e o que permite reter por
        DROP PARTITION em vez de DELETE em massa."""
        limpo.record_executions([
            ExecutionRecord(id=6000, started_at=_no_janela(0), workflow_id="w",
                            status="error")
        ])
        with limpo.db.connection() as conn:
            tabela = conn.execute(
                "SELECT tableoid::regclass::text AS t FROM execution WHERE id=6000"
            ).fetchone()["t"]
            eh_particionada = conn.execute(
                "SELECT count(*) AS n FROM pg_partitioned_table pt "
                "JOIN pg_class c ON c.oid = pt.partrelid "
                "WHERE c.relname = 'execution'"
            ).fetchone()["n"]
        assert eh_particionada == 1, "execution deixou de ser particionada"
        assert "execution_" in tabela, f"linha nao foi para uma particao: {tabela}"