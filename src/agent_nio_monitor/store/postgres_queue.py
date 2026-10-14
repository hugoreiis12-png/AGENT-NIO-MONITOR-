"""`NotificationQueue` no Postgres: mesmas regras da fila em memoria, com `FOR UPDATE SKIP LOCKED`."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from psycopg import sql

from .db import Database
from .queue import (
    ATUALIZADO,
    ENFILEIRADO,
    LEASE_SEGUNDOS,
    RETENCAO_DIAS,
    SUPRIMIDO,
    QueueItem,
    chave_de_dedup,
    evento_para_payload,
)
from .repository import EventRecord


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
                # COMMIT explicito: o `return` de dentro do `with` deixava a
                # transacao aberta para o pool. Funcionava por acaso porque a
                # pool commita no `__exit__` -- atencao que vire checkout puro
                # transformaria isto em transacao vazada na conexao.
                conn.commit()
                return SUPRIMIDO
            # `ON CONFLICT ... DO NOTHING` no indice parcial
            # `ux_queue_pendente_por_chave` (migration 004) fecha a corrida
            # UPDATE -> SELECT -> INSERT que existia aqui. Sem ele, dois
            # enqueues concorrentes da mesma chave de estado caem os dois no
            # INSERT; o perdedor levanta `UniqueViolation`, sobe ate
            # `EventPipeline.enqueue` e e engolido por `main._laco_do_job` --
            # o evento fica gravado em `event` e NUNCA e enfileirado. A
            # constraint convertia corrida em perda de dado em vez de retry.
            #
            # `rowcount` distingue os desfechos, entao nao ha excecao: quem
            # perdeu a corrida devolve ATUALIZADO, que e o que de fato houve.
            inseridos = conn.execute(
                sql.SQL(
                    "INSERT INTO {t} (id, event_id, dedup_key, event_type, resource_id, created_at, ready_at, payload) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb) "
                    "ON CONFLICT (dedup_key) WHERE status = 'pending' DO NOTHING"
                ).format(t=self._t()),
                (uuid.uuid4(), evento.id, chave, evento.event_type, evento.resource_id, now, ready_at, payload),
            ).rowcount
            conn.commit()
        return ENFILEIRADO if inseridos else ATUALIZADO

    def due(self, now: datetime, limit: int = 50) -> list[QueueItem]:
        """Reivindica ate `limit` itens prontos e RESERVA cada um.

        Antes era um `SELECT` puro: nao havia transicao de estado, entao dois
        despachantes (ou dois `flush` sobrepostos) liam as MESMAS linhas e
        mandavam o mesmo e-mail duas vezes. Seguro so porque `main._agenda`
        mantem um unico laco serializado -- nenhuma garantia no banco, nenhuma
        trava consultiva, nenhuma eleicao de lider.

        A reserva escreve `next_attempt_at = now + lease` no MESMO `UPDATE` que
        seleciona, com `FOR UPDATE SKIP LOCKED`. Locked: a segunda transacao pula
        a linha, em vez de bloquear esperando.

        Deliberadamente NAO cria um status `sending`: isso exigiria mexer no
        CHECK de `notification_queue.status` (migration 004) e daria uma
        terceira fonte de verdade sobre a fase do item. `next_attempt_at` ja
        significa "quando volta a ser elegivel", que e a mesma coisa.

        Efeito colateral boo: se o processo morrer no meio do envio, o item volta
        a ser elegivel sozinho quando o lease expira.
        """
        with self.db.connection() as conn:
            rows = conn.execute(
                sql.SQL(
                    "UPDATE {t} SET next_attempt_at = %s WHERE id IN ("
                    "  SELECT id FROM {t} WHERE status = 'pending' AND ready_at <= %s"
                    "  AND (next_attempt_at IS NULL OR next_attempt_at <= %s)"
                    "  ORDER BY ready_at LIMIT %s FOR UPDATE SKIP LOCKED"
                    ") RETURNING *"
                ).format(t=self._t()),
                (now + timedelta(seconds=LEASE_SEGUNDOS), now, now, limit),
            ).fetchall()
            conn.commit()
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
