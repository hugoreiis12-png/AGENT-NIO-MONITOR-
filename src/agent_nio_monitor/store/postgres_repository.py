"""Implementacao Postgres do `Repository`.

Redacao aplicada na fronteira de escrita: `redact()` e chamado aqui, antes do
COPY/INSERT. Assim nao existe caminho de codigo que grave segredo -- e o teste
`test_nenhum_segredo_chega_ao_banco` nao depende de disciplina do chamador.
"""

from __future__ import annotations

import json
from datetime import datetime

import structlog
from psycopg import sql

from ..redaction import redact
from .postgres_records import PostgresRecords
from .repository import (
    CursorState,
    ExecutionRecord,
    FailureRecord,
    Repository,
    Streak,
)

log = structlog.get_logger(__name__)


class PostgresRepository(PostgresRecords, Repository):
    """Cursor, execucoes, falhas e streak. Eventos e catalogo vem de `PostgresRecords`."""

    # ---------------------------------------------------------------- cursor

    def get_cursor(self, source: str, scope: str) -> CursorState | None:
        with self.db.connection() as conn:
            row = conn.execute(
                sql.SQL(
                    "SELECT source, scope, cursor, last_success_at, consecutive_failures "
                    "FROM {} WHERE source=%s AND scope=%s"
                ).format(self._t("source_cursor")),
                (source, scope),
            ).fetchone()
        if not row:
            return None
        return CursorState(
            source=row["source"],
            scope=row["scope"],
            cursor=row["cursor"],
            last_success_at=row["last_success_at"],
            consecutive_failures=row["consecutive_failures"],
        )

    def set_cursor(self, state: CursorState) -> None:
        with self.db.connection() as conn:
            conn.execute(
                sql.SQL(
                    """
                    INSERT INTO {t} (source, scope, cursor, last_success_at,
                                          last_attempt_at, consecutive_failures)
                    VALUES (%s, %s, %s, %s, now(), %s)
                    ON CONFLICT (source, scope) DO UPDATE
                    SET cursor = EXCLUDED.cursor,
                        last_success_at = EXCLUDED.last_success_at,
                        last_attempt_at = now(),
                        consecutive_failures = EXCLUDED.consecutive_failures
                    """
                ).format(t=self._t("source_cursor")),
                (state.source, state.scope, state.cursor, state.last_success_at,
                 state.consecutive_failures),
            )
            conn.commit()

    # ------------------------------------------------------------ executions

    def execution_exists(self, execution_id: int) -> bool:
        with self.db.connection() as conn:
            row = conn.execute(
                sql.SQL("SELECT 1 FROM {t} WHERE id = %s LIMIT 1").format(t=self._t("execution")),
                (execution_id,),
            ).fetchone()
        return row is not None

    def record_executions(self, records: list[ExecutionRecord]) -> int:
        """COPY direto. Lote de ~540/dia nao justifica INSERT row a row."""
        if not records:
            return 0
        # Lote via unnest + ON CONFLICT.
        #
        # A versao anterior usava `COPY ... FROM STDIN` e estava quebrada em tres
        # pontos: `columns="row"` nao existe em psycopg3 (assinatura real e
        # `(statement, params, *, writer)`), `cur.executemany()` nao alimenta o
        # COPY, e COPY nao tem ON CONFLICT -- re-inserir apos um reset de cursor
        # estourava UniqueViolation e derrubava a transacao inteira.
        #
        # Esta forma foi validada por execucao em PostgreSQL 16.4:
        # `unnest(ARRAY[...], ARRAY[...], ...) AS t(col, col, ...)`
        arrays = (
            [r.started_at for r in records],
            [r.id for r in records],
            [r.tenant_id for r in records],
            [r.workflow_id for r in records],
            [r.workflow_name for r in records],
            [r.status for r in records],
            [r.mode for r in records],
            [r.stopped_at for r in records],
            [r.duration_ms for r in records],
        )
        with self.db.connection() as conn:
            inserted = conn.execute(
                sql.SQL(
                    """
                    INSERT INTO {t} (started_at, id, tenant_id, workflow_id,
                                     workflow_name, status, mode, stopped_at, duration_ms)
                    SELECT * FROM unnest(
                        %s::timestamptz[], %s::bigint[], %s::text[], %s::text[],
                        %s::text[], %s::text[], %s::text[], %s::timestamptz[],
                        %s::integer[]
                    ) AS t(started_at, id, tenant_id, workflow_id, workflow_name,
                           status, mode, stopped_at, duration_ms)
                    ON CONFLICT (id, started_at) DO NOTHING
                    """
                ).format(t=self._t("execution")),
                arrays,
            )
            conn.commit()
            # rowcount devolve quantas linhas entraram de fato -- util para log
            # e para provar que o ON CONFLICT funciona de verdade.
            return inserted.rowcount or 0

    def record_failure(self, failure: FailureRecord) -> None:
        """`run_data` entra REDIGIDO. E o unico lugar do banco onde payload de
        workflow aparece, e por isso o redactor fica aqui, nao no chamador."""
        payload = json.dumps(redact(failure.run_data)) if failure.run_data is not None else None
        with self.db.connection() as conn:
            conn.execute(
                sql.SQL(
                    """
                    INSERT INTO {t} (started_at, id, workflow_id, tenant_id,
                                     error_msg, last_node, run_data, truncated)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (id, started_at) DO NOTHING
                    """
                ).format(t=self._t("execution_failure")),
                (
                    failure.started_at, failure.execution_id, failure.workflow_id, None,
                    failure.error_msg, failure.last_node, payload, failure.truncated,
                ),
            )
            conn.commit()

    # --------------------------------------------------------------- streaks

    def bump_streak(
        self, workflow_id: str, at: datetime, tenant_id: str | None, name: str | None
    ) -> Streak:
        """`count + 1` INCONDICIONAL -- nunca depende da ordem de chegada.

        A idempotencia NAO mora aqui e sim na triagem do coletor:
        `ExecutionErrorCollector._process` so adiciona a `novos` o que
        `execution_exists()` INSTANCE de ver. Reprocessar a mesma falha, seja por
        replay de coleta ou por cursor invalidado, nao chega aqui.

        Ja houve uma tentativa de pôr a chave aqui -- guardar a ultima execucao
        contada na propria tabela. Ela foi removida depois de medida: so barra
        repeticao CONSECUTIVA, e um replay de sequencia (10, 11, 12 seguido de
        10, 11, 12) somava 6 em vez de 3. Dava uma sensacao de protecao que nao
        existia. Ver ADR D25.

        `first_at` usa LEAST para casar com a InMemory: se um erro chega com
        timestamp anterior ao ja registrado, a janela do streak tem que crescer
        para tras. Sem isso, `primeira` no email contava errado.
        """
        with self.db.connection() as conn:
            row = conn.execute(
                sql.SQL(
                    """
                    INSERT INTO {t} (workflow_id, tenant_id, workflow_name, count, first_at, last_at)
                    VALUES (%s, %s, %s, 1, %s, %s)
                    ON CONFLICT (workflow_id) DO UPDATE
                    SET count = {tn}.workflow_streak.count + 1,
                        first_at = LEAST({tn}.workflow_streak.first_at, EXCLUDED.first_at),
                        last_at = GREATEST({tn}.workflow_streak.last_at, EXCLUDED.last_at),
                        tenant_id = COALESCE(EXCLUDED.tenant_id, {tn}.workflow_streak.tenant_id),
                        workflow_name = COALESCE(EXCLUDED.workflow_name, {tn}.workflow_streak.workflow_name),
                        resolved_at = NULL
                    RETURNING workflow_id, tenant_id, workflow_name, count, first_at, last_at,
                              last_notified_at, resolved_at
                    """
                ).format(t=self._t("workflow_streak"), tn=sql.Identifier(self.schema)),
                (workflow_id, tenant_id, name, at, at),
            ).fetchone()
            conn.commit()
        return Streak(
            workflow_id=row["workflow_id"],
            count=row["count"],
            first_at=row["first_at"],
            last_at=row["last_at"],
            tenant_id=row["tenant_id"],
            workflow_name=row["workflow_name"],
            last_notified_at=row["last_notified_at"],
            resolved_at=row["resolved_at"],
        )

    def get_streak(self, workflow_id: str) -> Streak | None:
        # tenant_id e workflow_name entao no SELECT de proposito: sem eles, o
        # objeto devolvido aqui NAO tinha os campos que a InMemory devolvia, e o
        # nome do workflow sumia em producao.
        with self.db.connection() as conn:
            row = conn.execute(
                sql.SQL(
                    "SELECT workflow_id, count, first_at, last_at, last_notified_at, "
                    "tenant_id, workflow_name, resolved_at "
                    "FROM {t} WHERE workflow_id=%s"
                ).format(t=self._t("workflow_streak")),
                (workflow_id,),
            ).fetchone()
        if not row:
            return None
        return Streak(
            workflow_id=row["workflow_id"],
            count=row["count"],
            first_at=row["first_at"],
            last_at=row["last_at"],
            tenant_id=row["tenant_id"],
            workflow_name=row["workflow_name"],
            last_notified_at=row["last_notified_at"],
            resolved_at=row["resolved_at"],
        )

    def mark_streak_notified(self, workflow_id: str, at: datetime) -> None:
        with self.db.connection() as conn:
            conn.execute(
                sql.SQL("UPDATE {t} SET last_notified_at=%s WHERE workflow_id=%s").format(
                    t=self._t("workflow_streak")
                ),
                (at, workflow_id),
            )
            conn.commit()

    def resolve_streak(self, workflow_id: str, at: datetime) -> None:
        with self.db.connection() as conn:
            conn.execute(
                sql.SQL("DELETE FROM {t} WHERE workflow_id=%s").format(t=self._t("workflow_streak")),
                (workflow_id,),
            )
            conn.commit()
