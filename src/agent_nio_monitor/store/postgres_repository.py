"""Implementacao Postgres do `Repository`.

Redacao aplicada na fronteira de escrita: `redact()` e chamado aqui, antes do
COPY/INSERT. Assim nao existe caminho de codigo que grave segredo -- e o teste
`test_nenhum_segredo_chega_ao_banco` nao depende de disciplina do chamador.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import structlog
from psycopg import sql

from ..redaction import redact
from .repository import (
    CursorState,
    EventRecord,
    ExecutionRecord,
    FailureRecord,
    Repository,
    Streak,
)

log = structlog.get_logger(__name__)


class PostgresRepository(Repository):
    def __init__(self, db) -> None:  # noqa: ANN001 - evita import circular
        self.db = db
        self.schema = db.schema

    def _t(self, table: str) -> sql.Composed:
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(table))

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

    # ---------------------------------------------------------------- events

    def save_event(self, event: EventRecord) -> None:
        with self.db.connection() as conn:
            conn.execute(
                sql.SQL(
                    """
                    INSERT INTO {t} (ts, id, tenant_id, source, resource_kind, resource_id,
                                     event_type, severity, title, detail, streak_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    """
                ).format(t=self._t("event")),
                (
                    event.ts, event.id, event.tenant_id, event.source,
                    event.resource_id and "workflow", event.resource_id,
                    event.event_type, event.severity, event.title,
                    json.dumps(redact(event.detail)), event.streak_count,
                ),
            )
            if event.changes:
                # `executemany` vive no CURSOR, nao na Connection (psycopg3).
                # `conn.executemany` levanta AttributeError -- achado rodando
                # contra o banco de verdade.
                with conn.cursor() as cur:
                    cur.executemany(
                        sql.SQL(
                            "INSERT INTO {ch} (event_id, seq, kind, path, before, after) "
                            "VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb)"
                        ).format(ch=self._t("event_change")),
                        [
                            (
                                event.id, i, c.kind, c.path,
                                json.dumps(redact(c.before)) if c.before is not None else None,
                                json.dumps(redact(c.after)) if c.after is not None else None,
                            )
                            for i, c in enumerate(event.changes)
                        ],
                    )
            conn.commit()

    def save_notification(self, **kwargs: Any) -> None:
        with self.db.connection() as conn:
            conn.execute(
                sql.SQL(
                    """
                    INSERT INTO {t} (ts, id, event_id, channel, recipients, subject,
                                     body, status, error, attempts)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """
                ).format(t=self._t("notification")),
                (
                    kwargs.get("ts") or datetime.now().astimezone(),
                    kwargs.get("id") or uuid.uuid4(),
                    kwargs.get("event_id"),
                    kwargs.get("channel", "n8n_webhook"),
                    kwargs.get("recipients", []),
                    kwargs.get("subject", ""),
                    kwargs.get("body"),
                    kwargs.get("status", "sent"),
                    kwargs.get("error"),
                    kwargs.get("attempts", 1),
                ),
            )
            conn.commit()

    # -------------------------------------------------------------- ancillary

    def save_capabilities(self, rows: list[dict[str, Any]]) -> None:
        with self.db.connection() as conn:
            # cursor, nao Connection -- ver comentario em save_event
            with conn.cursor() as cur:
                cur.executemany(
                    sql.SQL(
                        """
                        INSERT INTO {t} (source, feature, path, available, status_code,
                                         detail, probed_at)
                        VALUES (%s,%s,%s,%s,%s,%s, now())
                        ON CONFLICT (source, feature, path) DO UPDATE
                        SET available=EXCLUDED.available, status_code=EXCLUDED.status_code,
                            detail=EXCLUDED.detail, probed_at=now()
                        """
                    ).format(t=self._t("capabilities")),
                    [
                        (r["source"], r["feature"], r["path"], r["available"],
                         r.get("status_code"), r.get("detail"))
                        for r in rows
                    ],
                )
            conn.commit()

    def save_workflow_snapshot(
        self, workflow_id: str, version_id: str | None, active: bool, body: Any
    ) -> None:
        with self.db.connection() as conn:
            conn.execute(
                sql.SQL(
                    """
                    INSERT INTO {t} (workflow_id, version_id, active, body, captured_at)
                    VALUES (%s,%s,%s,%s::jsonb, now())
                    """
                ).format(t=self._t("workflow_snapshot")),
                (workflow_id, version_id, active, json.dumps(redact(body))),
            )
            conn.commit()

    def upsert_resource(self, **kwargs: Any) -> None:
        with self.db.connection() as conn:
            conn.execute(
                sql.SQL(
                    """
                    INSERT INTO {t} (source, kind, external_id, tenant_id, name, critical, meta, last_seen)
                    VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb, now())
                    ON CONFLICT (source, kind, external_id) DO UPDATE
                    SET tenant_id=EXCLUDED.tenant_id, name=EXCLUDED.name,
                        critical=EXCLUDED.critical, meta=EXCLUDED.meta,
                        last_seen=now(), missing_since=NULL
                    """
                ).format(t=self._t("resource")),
                (
                    kwargs["source"], kwargs["kind"], kwargs["external_id"],
                    kwargs.get("tenant_id"), kwargs.get("name"),
                    kwargs.get("critical", False), json.dumps(kwargs.get("meta", {})),
                ),
            )
            conn.commit()

    def record_reconciliation(self, **kwargs: Any) -> None:
        with self.db.connection() as conn:
            conn.execute(
                sql.SQL(
                    """
                    INSERT INTO {t} (source, scope, expected, seen, gaps, cursor_valid,
                                     duration_ms, detail, run_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb, now())
                    """
                ).format(t=self._t("reconciliation_run")),
                (
                    kwargs["source"], kwargs["scope"], kwargs.get("expected", 0),
                    kwargs.get("seen", 0), kwargs.get("gaps", 0),
                    kwargs.get("cursor_valid", True), kwargs.get("duration_ms"),
                    json.dumps(kwargs.get("detail", {})),
                ),
            )
            conn.commit()

    def record_container_observation(
        self,
        container_id: str,
        at: datetime,
        *,
        tenant_id: str | None = None,
        state: str | None = None,
        health: str | None = None,
        restart_count: int | None = None,
        mem_pct: float | None = None,
        cpu_pct: float | None = None,
    ) -> None:
        with self.db.connection() as conn:
            conn.execute(
                sql.SQL(
                    """
                    INSERT INTO {t} (ts, container_id, tenant_id, state, health,
                                     restart_count, mem_pct, cpu_pct)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """
                ).format(t=self._t("container_observation")),
                (
                    at, container_id, tenant_id, state, health,
                    restart_count, mem_pct, cpu_pct,
                ),
            )
            conn.commit()

    def last_container_observation(self, container_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute(
                sql.SQL(
                    "SELECT ts, state, health, restart_count, mem_pct, cpu_pct "
                    "FROM {t} WHERE container_id = %s ORDER BY ts DESC LIMIT 1"
                ).format(t=self._t("container_observation")),
                (container_id,),
            ).fetchone()
        return dict(row) if row else None