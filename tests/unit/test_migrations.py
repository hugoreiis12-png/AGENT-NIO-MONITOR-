"""Valida o DDL com um parser Postgres de verdade.

Docker nao esta disponivel nesta maquina, entao o DDL nao pode ser aplicado
num banco efemero. Isso NAO e desculpa para nao validar: `pglast` faz parse
usando o proprio grammar do PostgreSQL. O que ele pega: erro de sintaxe,
tipo invalido, coluna inexistente em indice, expressao malformada.

O que ele NAO pega (e precisa de banco de verdade, ver README):
  - constraint de FK entre tabela particionada
  - colisao de nome de particao
  - comportamento em volume
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pglast import parse_sql

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def _statements(path: Path) -> list:
    return parse_sql(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", sorted(MIGRATIONS.glob("*.sql")))
def test_migration_parseia(path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    assert sql.strip(), f"{path.name} esta vazio"
    stmts = _statements(path)
    assert len(stmts) > 0, f"{path.name} nao produziu nenhum statement"


def test_schema_inicial_tem_as_tabelas_que_o_codigo_usa() -> None:
    sql = (MIGRATIONS / "001_initial.sql").read_text(encoding="utf-8")
    for table in (
        "source_cursor",
        "capabilities",
        "resource",
        "workflow_streak",
        "execution",
        "execution_failure",
        "event",
        "event_change",
        "workflow_snapshot",
        "container_observation",
        "notification",
        "reconciliation_run",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql, f"falta tabela {table}"


def test_tabelas_de_serie_temporal_sao_particionadas() -> None:
    """Sem PARTITION BY RANGE nao ha como reter por DROP PARTITION -- que e o
    ponto de usar Postgres aqui."""
    sql = (MIGRATIONS / "001_initial.sql").read_text(encoding="utf-8")
    for table in (
        "execution",
        "execution_failure",
        "event",
        "workflow_snapshot",
        "container_observation",
        "notification",
    ):
        bloco = sql.split(f"CREATE TABLE IF NOT EXISTS {table} (", 1)[1].split(";", 1)[0]
        assert "PARTITION BY RANGE" in bloco, f"{table} nao e particionada"


def test_retencao_usa_drop_e_nao_delete() -> None:
    sql = (MIGRATIONS / "001_initial.sql").read_text(encoding="utf-8")
    assert "DROP TABLE IF EXISTS hermes.%I" in sql
    funcao_retencao = sql.split("drop_partitions_older_than", 1)[1]
    assert "DELETE FROM" not in funcao_retencao


def test_indice_parcial_das_falhas_existe() -> None:
    """E a consulta que todo relatorio de falha faz."""
    sql = (MIGRATIONS / "001_initial.sql").read_text(encoding="utf-8")
    assert "ix_execution_failed" in sql
    assert "WHERE status <> 'success'" in sql