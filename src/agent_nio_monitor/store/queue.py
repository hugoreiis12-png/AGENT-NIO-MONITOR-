"""Fila interna de disparo: o alerta espera um ciclo e so sai se o problema continua.

Regras (iguais em memoria e no Postgres):
- no maximo UM item pendente por `dedup_key`; item novo com a mesma chave so atualiza o
  conteudo (ex.: a sequencia de falhas cresceu), nao duplica;
- chave ja ENVIADA dentro da janela de re-notificacao nao entra de novo.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from psycopg import sql

from ..redaction import redact
from .db import Database
from .repository import EventRecord

PENDING, SENT, DISCARDED, FAILED = "pending", "sent", "discarded", "failed"
ENFILEIRADO, ATUALIZADO, SUPRIMIDO = "enfileirado", "atualizado", "suprimido"
RETENCAO_DIAS = 30

# Estado que pode voltar ao normal: o mesmo problema nao se repete na janela de
# re-notificacao e dois avisos pendentes do mesmo problema viram um so. Os demais
# eventos (workflow alterado, stack alterada...) sao FATOS: cada um e um aviso proprio.
EVENTOS_DE_ESTADO = frozenset({
    "execution_failed", "container_stopped", "container_unhealthy", "container_removed",
    "container_restart_loop", "container_resource_high",
})


@dataclass
class QueueItem:
    id: uuid.UUID
    event_id: uuid.UUID
    dedup_key: str
    event_type: str
    resource_id: str | None
    created_at: datetime
    ready_at: datetime
    payload: dict[str, Any]
    status: str = PENDING
    attempts: int = 0
    next_attempt_at: datetime | None = None
    sent_at: datetime | None = None
    last_error: str | None = None
    resolved_reason: str | None = None


def evento_para_payload(evento: EventRecord) -> dict[str, Any]:
    """Dados que o despacho precisa para remontar o evento (sem as `changes`,
    que ja estao em `event_change`). Redigido: o payload vai para o banco."""
    return redact({
        "id": str(evento.id), "ts": evento.ts.isoformat(), "source": evento.source,
        "event_type": evento.event_type, "severity": evento.severity, "title": evento.title,
        "tenant_id": evento.tenant_id, "resource_id": evento.resource_id,
        "streak_count": evento.streak_count, "detail": evento.detail,
    })


def payload_para_evento(payload: dict[str, Any]) -> EventRecord:
    return EventRecord(
        id=uuid.UUID(payload["id"]), ts=datetime.fromisoformat(payload["ts"]), source=payload["source"],
        event_type=payload["event_type"], severity=payload["severity"], title=payload["title"],
        tenant_id=payload.get("tenant_id"), resource_id=payload.get("resource_id"),
        streak_count=payload.get("streak_count", 1), detail=payload.get("detail") or {},
    )


def chave_de_dedup(evento: EventRecord) -> str:
    base = f"{evento.event_type}:{evento.resource_id or '-'}"
    return base if evento.event_type in EVENTOS_DE_ESTADO else f"{base}:{evento.id}"


class NotificationQueue(Protocol):
    def enqueue(self, evento: EventRecord, *, ready_at: datetime, renotify_minutes: int, now: datetime) -> str: ...
    def due(self, now: datetime, limit: int = 50) -> list[QueueItem]: ...
    def mark_sent(self, item_id: uuid.UUID, now: datetime) -> None: ...
    def mark_discarded(self, item_id: uuid.UUID, reason: str, now: datetime) -> None: ...
    def mark_retry(self, item_id: uuid.UUID, error: str, next_attempt_at: datetime) -> None: ...
    def mark_failed(self, item_id: uuid.UUID, error: str) -> None: ...
    def stats(self, now: datetime) -> dict[str, Any]: ...
    def purge(self, now: datetime) -> int: ...


@dataclass
class InMemoryQueue:
    """Mesma semantica do Postgres; usada nos testes e no comando `once`."""

    items: list[QueueItem] = field(default_factory=list)

    def enqueue(self, evento: EventRecord, *, ready_at: datetime, renotify_minutes: int, now: datetime) -> str:
        chave = chave_de_dedup(evento)
        pendente = next((i for i in self.items if i.dedup_key == chave and i.status == PENDING), None)
        if pendente is not None:
            pendente.payload = evento_para_payload(evento)
            return ATUALIZADO
        janela = timedelta(minutes=renotify_minutes)
        if renotify_minutes > 0 and any(
            i.dedup_key == chave and i.status == SENT and i.sent_at and now - i.sent_at < janela for i in self.items
        ):
            return SUPRIMIDO
        self.items.append(QueueItem(
            id=uuid.uuid4(), event_id=evento.id, dedup_key=chave, event_type=evento.event_type,
            resource_id=evento.resource_id, created_at=now, ready_at=ready_at, payload=evento_para_payload(evento),
        ))
        return ENFILEIRADO

    def due(self, now: datetime, limit: int = 50) -> list[QueueItem]:
        prontos = [
            i for i in self.items
            if i.status == PENDING and i.ready_at <= now and (i.next_attempt_at is None or i.next_attempt_at <= now)
        ]
        return sorted(prontos, key=lambda i: i.ready_at)[:limit]

    def _get(self, item_id: uuid.UUID) -> QueueItem:
        return next(i for i in self.items if i.id == item_id)

    def mark_sent(self, item_id: uuid.UUID, now: datetime) -> None:
        item = self._get(item_id)
        item.status, item.sent_at = SENT, now

    def mark_discarded(self, item_id: uuid.UUID, reason: str, now: datetime) -> None:
        item = self._get(item_id)
        item.status, item.resolved_reason = DISCARDED, reason

    def mark_retry(self, item_id: uuid.UUID, error: str, next_attempt_at: datetime) -> None:
        item = self._get(item_id)
        item.attempts += 1
        item.last_error, item.next_attempt_at = error, next_attempt_at

    def mark_failed(self, item_id: uuid.UUID, error: str) -> None:
        item = self._get(item_id)
        item.status, item.last_error = FAILED, error

    def stats(self, now: datetime) -> dict[str, Any]:
        pendentes = [i for i in self.items if i.status == PENDING]
        mais_antigo = max((int((now - i.created_at).total_seconds()) for i in pendentes), default=0)
        return {"pending": len(pendentes), "oldest_pending_seconds": mais_antigo}

    def purge(self, now: datetime) -> int:
        limite = now - timedelta(days=RETENCAO_DIAS)
        antes = len(self.items)
        self.items = [i for i in self.items if i.status == PENDING or i.created_at >= limite]
        return antes - len(self.items)


class PostgresQueue:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.schema = db.schema

    def _t(self) -> sql.Composed:
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier("notification_queue"))

    def enqueue(self, evento: EventRecord, *, ready_at: datetime, renotify_minutes: int, now: datetime) -> str:
        chave = chave_de_dedup(evento)
        payload = json.dumps(evento_para_payload(evento))
        with self.db.connection() as conn:
            atualizados = conn.execute(
                sql.SQL("UPDATE {t} SET payload = %s::jsonb WHERE dedup_key = %s AND status = 'pending'").format(t=self._t()),
                (payload, chave),
            ).rowcount
            if atualizados:
                conn.commit()
                return ATUALIZADO
            if renotify_minutes > 0 and conn.execute(
                sql.SQL(
                    "SELECT 1 FROM {t} WHERE dedup_key = %s AND status = 'sent' AND sent_at > %s LIMIT 1"
                ).format(t=self._t()),
                (chave, now - timedelta(minutes=renotify_minutes)),
            ).fetchone():
                return SUPRIMIDO
            conn.execute(
                sql.SQL(
                    "INSERT INTO {t} (id, event_id, dedup_key, event_type, resource_id, created_at, ready_at, payload) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)"
                ).format(t=self._t()),
                (uuid.uuid4(), evento.id, chave, evento.event_type, evento.resource_id, now, ready_at, payload),
            )
            conn.commit()
        return ENFILEIRADO

    def due(self, now: datetime, limit: int = 50) -> list[QueueItem]:
        with self.db.connection() as conn:
            rows = conn.execute(
                sql.SQL(
                    "SELECT * FROM {t} WHERE status = 'pending' AND ready_at <= %s "
                    "AND (next_attempt_at IS NULL OR next_attempt_at <= %s) ORDER BY ready_at LIMIT %s"
                ).format(t=self._t()),
                (now, now, limit),
            ).fetchall()
        return [
            QueueItem(
                id=r["id"], event_id=r["event_id"], dedup_key=r["dedup_key"], event_type=r["event_type"],
                resource_id=r["resource_id"], created_at=r["created_at"], ready_at=r["ready_at"], payload=r["payload"],
                status=r["status"], attempts=r["attempts"], next_attempt_at=r["next_attempt_at"],
            )
            for r in rows
        ]

    def _update(self, item_id: uuid.UUID, assignments: str, params: tuple) -> None:
        with self.db.connection() as conn:
            conn.execute(sql.SQL(f"UPDATE {{t}} SET {assignments} WHERE id = %s").format(t=self._t()), (*params, item_id))
            conn.commit()

    def mark_sent(self, item_id: uuid.UUID, now: datetime) -> None:
        self._update(item_id, "status = 'sent', sent_at = %s", (now,))

    def mark_discarded(self, item_id: uuid.UUID, reason: str, now: datetime) -> None:
        self._update(item_id, "status = 'discarded', resolved_reason = %s", (reason,))

    def mark_retry(self, item_id: uuid.UUID, error: str, next_attempt_at: datetime) -> None:
        self._update(item_id, "attempts = attempts + 1, last_error = %s, next_attempt_at = %s", (error, next_attempt_at))

    def mark_failed(self, item_id: uuid.UUID, error: str) -> None:
        self._update(item_id, "status = 'failed', last_error = %s", (error,))

    def stats(self, now: datetime) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute(
                sql.SQL("SELECT count(*) AS n, min(created_at) AS antigo FROM {t} WHERE status = 'pending'").format(t=self._t())
            ).fetchone()
        antigo = row["antigo"]
        return {"pending": row["n"], "oldest_pending_seconds": int((now - antigo).total_seconds()) if antigo else 0}

    def purge(self, now: datetime) -> int:
        with self.db.connection() as conn:
            removidos = conn.execute(
                sql.SQL("DELETE FROM {t} WHERE status <> 'pending' AND created_at < %s").format(t=self._t()),
                (now - timedelta(days=RETENCAO_DIAS),),
            ).rowcount
            conn.commit()
        return removidos


def agora() -> datetime:
    return datetime.now(UTC)
