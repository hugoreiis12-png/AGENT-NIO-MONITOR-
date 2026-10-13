"""Conexao Postgres e runner de migration.

Regra do projeto: nunca no database do n8n. `schema` separado, ver config.
"""

from __future__ import annotations

from pathlib import Path

import structlog
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = structlog.get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


class Database:
    def __init__(self, dsn: str, schema: str = "hermes", min_size: int = 1, max_size: int = 8):
        self.schema = schema
        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={"options": f"-c search_path={schema},public", "row_factory": dict_row},
        )

    def open(self) -> None:
        self.pool.open(wait=True, timeout=30)
        log.info("pool_postgres_aberto", schema=self.schema, **self.pool.get_stats())

    def close(self) -> None:
        self.pool.close()

    def connection(self):
        return self.pool.connection()

    def ensure_partitions(self, months_ahead: int = 2) -> None:
        with self.pool.connection() as conn:
            conn.execute(sql.SQL("SELECT hermes.ensure_partitions({})").format(months_ahead))
            conn.commit()
            log.info("particoes_garantidas", meses_a_frente=months_ahead)

    def migrate(self, directory: Path | None = None) -> list[str]:
        """Aplica migrations na ordem, uma vez cada.

        Versionamento por tabela `schema_migrations`. A versao anterior reaplicava
        todos os arquivos a cada boot e so confiava no `IF NOT EXISTS` do DDL --
        suficiente enquanto so existisse `001`, mas a partir da segunda migration
        um `ALTER` nao seria idempotente.

        Nao usa transactiona por arquivo: `CREATE INDEX CONCURRENTLY` e
        `DROP TABLE` nao podem rodar dentro de transaction, e reexecutar uma
        migration ja aplicada e seguro por construcao (o registro impede).
        """
        directory = directory or MIGRATIONS_DIR
        with self.pool.connection() as conn:
            conn.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {s}.schema_migrations (
                        version    TEXT PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        checksum   TEXT
                    )
                    """
                ).format(s=sql.Identifier(self.schema))
            )
            conn.commit()
            ja_aplicadas = {
                r["version"] for r in conn.execute(
                    sql.SQL("SELECT version FROM {s}.schema_migrations").format(
                        s=sql.Identifier(self.schema)
                    )
                ).fetchall()
            }

        applied: list[str] = []
        for path in sorted(directory.glob("*.sql")):
            if path.stem in ja_aplicadas:
                log.debug("migration_ja_aplicada", arquivo=path.name)
                continue
            corpo = path.read_text(encoding="utf-8")
            with self.pool.connection() as conn:
                try:
                    conn.execute(corpo)
                    conn.execute(
                        sql.SQL(
                            "INSERT INTO {s}.schema_migrations (version, checksum) "
                            "VALUES (%s, md5(%s))"
                        ).format(s=sql.Identifier(self.schema)),
                        (path.stem, corpo),
                    )
                    conn.commit()
                except Exception as exc:  # noqa: BLE001 -- erro de migration precisa nomear o arquivo
                    conn.rollback()
                    log.error("migration_falhou", arquivo=path.name, erro=str(exc))
                    raise
            applied.append(path.name)
            log.info("migration_aplicada", arquivo=path.name)
        return applied

    def drop_partitions_older_than(self, days: int) -> list[tuple[str, str]]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                sql.SQL("SELECT * FROM hermes.drop_partitions_older_than({})").format(days)
            ).fetchall()
            conn.commit()
        dropped = [(r["parent"], r["partition"]) for r in rows]
        if dropped:
            log.warning("particoes_removidas", qtd=len(dropped), detalhe=dropped)
        return dropped