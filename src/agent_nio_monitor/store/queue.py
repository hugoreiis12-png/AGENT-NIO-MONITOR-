"""Fila interna de disparo: o alerta espera um ciclo e so sai se o problema continua.

Regras (iguais em memoria e no Postgres):
- no maximo UM item pendente por `dedup_key`; item novo com a mesma chave so atualiza o
  conteudo (ex.: a sequencia de falhas cresceu), nao duplica;
- chave ja ENVIADA dentro da janela de re-notificacao nao entra de novo.

`reconciliation_gap` entrou em `EVENTOS_DE_ESTADO` porque e estado, nao fato: um
buraco que se repete a cada ciclo de coleta (o prune do n8n leva dias para ser
resolvido) deve virar UM alerta pendente, nao um e-mail por ciclo. Sem isso a
chave receberia o `evento.id` e nunca deduplicaria.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol


from ..redaction import redact
from .repository import EventRecord

PENDING, SENT, DISCARDED, FAILED = "pending", "sent", "discarded", "failed"
ENFILEIRADO, ATUALIZADO, SUPRIMIDO = "enfileirado", "atualizado", "suprimido"
RETENCAO_DIAS = 30

# Reserva do `due()`: um item so pode ser reivindicado uma vez por lease, e
# dois despachantes concorrentes nao pegam a mesma linha.
#
# Maior que o backoff maximo de reenvio (240 s, `dispatcher.ESPERAS_SEGUNDOS`)
# e maior que o timeout do LLM (60 s) somado as 3 tentativas do webhook -- um
# lease curto faria um segundo despachante roubar o item ainda em voo e
# duplicar o e-mail, que e exatamente o que a reserva existe para impedir.
LEASE_SEGUNDOS = 300

# Estado que pode voltar ao normal: o mesmo problema nao se repete na janela de
# re-notificacao e dois avisos pendentes do mesmo problema viram um so. Os demais
# eventos (workflow alterado, stack alterada...) sao FATOS: cada um e um aviso proprio.
EVENTOS_DE_ESTADO = frozenset({
    "execution_failed", "container_stopped", "container_unhealthy", "container_removed",
    "container_restart_loop", "container_resource_high", "reconciliation_gap", "desktop_flow_failed",
    "node_not_executed", "node_error_hidden", "workflow_trigger_failed", "container_log_error",
    "desktop_flow_handled_errors",
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


# Alerta com incidente (o inicio do problema na chave) nao se repete enquanto o incidente
# durar: so um incidente NOVO (o problema passou e voltou) alerta de novo. Medido em
# 19/10/2026: o mesmo RPA quebrado gerou 9 e-mails, um a cada 12 h de janela vencida.
SEM_REPETICAO_MINUTOS = RETENCAO_DIAS * 24 * 60


def chave_de_dedup(evento: EventRecord) -> str:
    base = f"{evento.event_type}:{evento.resource_id or '-'}"
    if evento.event_type not in EVENTOS_DE_ESTADO:
        return f"{base}:{evento.id}"
    incidente = evento.detail.get("incidente")
    return f"{base}:{incidente}" if incidente else base


def janela_de_renotificacao(evento: EventRecord, da_regra: int) -> int:
    return SEM_REPETICAO_MINUTOS if evento.detail.get("incidente") else da_regra


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
        """Reserva os itens entregues, como o `PostgresQueue`.

        A reserva e o que `test_paridade_postgres.py` compara entre as duas
        implementacoes. Aqui ela nao evita corrida (o processo e single-thread),
        mas mantem a semantica igual -- inclusive o detalhe de que um item
        devolvido ja vem `next_attempt_at` no futuro, para que um segundo
        `due()` nao o entregue de novo.
        """
        prontos = sorted(
            (
                i for i in self.items
                if i.status == PENDING and i.ready_at <= now
                and (i.next_attempt_at is None or i.next_attempt_at <= now)
            ),
            key=lambda i: i.ready_at,
        )[:limit]
        for item in prontos:
            item.next_attempt_at = now + timedelta(seconds=LEASE_SEGUNDOS)
        return prontos

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


def agora() -> datetime:
    return datetime.now(UTC)
