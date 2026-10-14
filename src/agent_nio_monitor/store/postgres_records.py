"""Escritas de registro no Postgres: eventos, notificacoes, capacidades, snapshots,
recursos, reconciliacao e observacoes de container.

Mesma regra do `PostgresRepository`: `redact()` na fronteira de escrita.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from psycopg import sql

from ..redaction import redact
from .repository import EventRecord


class PostgresBase:
    def __init__(self, db) -> None:  # noqa: ANN001 - evita import circular
        self.db = db
        self.schema = db.schema

    def _t(self, table: str) -> sql.Composed:
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(table))


class PostgresRecords(PostgresBase):
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

    def last_workflow_snapshot(self, workflow_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute(
                sql.SQL(
                    "SELECT workflow_id, version_id, active, body FROM {t} "
                    "WHERE workflow_id = %s ORDER BY captured_at DESC LIMIT 1"
                ).format(t=self._t("workflow_snapshot")),
                (workflow_id,),
            ).fetchone()
        return dict(row) if row else None

    def has_workflow_snapshots(self) -> bool:
        with self.db.connection() as conn:
            row = conn.execute(
                sql.SQL("SELECT 1 FROM {t} LIMIT 1").format(t=self._t("workflow_snapshot"))
            ).fetchone()
        return row is not None

    def list_resources(self, source: str, kind: str) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                sql.SQL(
                    "SELECT external_id, name, tenant_id, meta FROM {t} WHERE source = %s AND kind = %s"
                ).format(t=self._t("resource")),
                (source, kind),
            ).fetchall()
        return [dict(r) for r in rows]

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
