"""`InMemoryRepository`: a MESMA semantica do Postgres, sem banco.

Nao e mock: dedup por id, streak, cursor e redacao iguais, e por isso exercita o
pipeline de verdade. `tests/integration/test_paridade_postgres.py` trava a paridade.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..redaction import redact
from .repository import CursorState, EventRecord, ExecutionRecord, FailureRecord, Repository, Streak


class InMemoryRepository(Repository):
    """Implementa a MESMA semantica do Postgres. Nao e mock."""

    def __init__(self) -> None:
        self.cursors: dict[tuple[str, str], CursorState] = {}
        self.executions: dict[int, ExecutionRecord] = {}
        self.failures: dict[int, FailureRecord] = {}
        self.streaks: dict[str, Streak] = {}
        self.events: list[EventRecord] = []
        self.notifications: list[dict[str, Any]] = []
        self.capabilities: list[dict[str, Any]] = []
        self.snapshots: dict[str, list[dict[str, Any]]] = {}
        self.resources: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.reconciliations: list[dict[str, Any]] = []
        self.container_observations: dict[str, list[dict[str, Any]]] = {}

    def get_cursor(self, source: str, scope: str) -> CursorState | None:
        return self.cursors.get((source, scope))

    def set_cursor(self, state: CursorState) -> None:
        self.cursors[(state.source, state.scope)] = state

    def execution_exists(self, execution_id: int) -> bool:
        return execution_id in self.executions

    def record_executions(self, records: list[ExecutionRecord]) -> int:
        novos = 0
        for r in records:
            if r.id in self.executions:
                continue
            self.executions[r.id] = r
            novos += 1
        return novos

    def record_failure(self, failure: FailureRecord) -> None:
        # Mesmo contrato do PostgresRepository: o repositorio redige. Se um dia
        # alguem implementa um terceiro backend, o padrao e redigir.
        self.failures[failure.execution_id] = FailureRecord(
            execution_id=failure.execution_id,
            started_at=failure.started_at,
            workflow_id=failure.workflow_id,
            error_msg=redact(failure.error_msg) if failure.error_msg else failure.error_msg,
            last_node=failure.last_node,
            run_data=redact(failure.run_data) if failure.run_data is not None else None,
            truncated=failure.truncated,
        )

    def bump_streak(
        self, workflow_id: str, at: datetime, tenant_id: str | None, name: str | None
    ) -> Streak:
        """Acumula SEMPRE. Nao reseta por ordem de chegada.

        A versão anterior fazia `if at > s.last_at: reinicia com count=0`. Como o
        n8n devolve as execucoes do mais novo para o mais antigo, e como um
        workflow que comeca a falhar gera erros com timestamp CRESCENTE, isso
        zerava o contador a cada falha: `count` ficava sempre 1 e a regra
        `min_streak: 3` (severity critical) NUNCA disparava.

        A idempotencia NAO mora aqui. Ela esta na triagem do coletor:
        `_process` so adiciona a `novos` o que `execution_exists()`.instance de
        verificar. Ver ADR D25.
        """
        s = self.streaks.get(workflow_id)
        if s is None:
            s = Streak(
                workflow_id=workflow_id, count=0, first_at=at, last_at=at,
                tenant_id=tenant_id, workflow_name=name,
            )
            self.streaks[workflow_id] = s
        s.count += 1
        if s.first_at is None or at < s.first_at:
            s.first_at = at
        if s.last_at is None or at > s.last_at:
            s.last_at = at
        s.tenant_id = tenant_id or s.tenant_id
        s.workflow_name = name or s.workflow_name
        return s

    def get_streak(self, workflow_id: str) -> Streak | None:
        return self.streaks.get(workflow_id)

    def mark_streak_notified(self, workflow_id: str, at: datetime) -> None:
        if s := self.streaks.get(workflow_id):
            s.last_notified_at = at

    def resolve_streak(self, workflow_id: str, at: datetime) -> None:
        self.streaks.pop(workflow_id, None)

    def save_event(self, event: EventRecord) -> None:
        self.events.append(event)

    def save_notification(self, **kwargs: Any) -> None:
        self.notifications.append(kwargs)

    def save_capabilities(self, rows: list[dict[str, Any]]) -> None:
        self.capabilities = rows

    def save_workflow_snapshot(
        self, workflow_id: str, version_id: str | None, active: bool, body: Any
    ) -> None:
        self.snapshots.setdefault(workflow_id, []).append(
            {
                "workflow_id": workflow_id,
                "version_id": version_id,
                "active": active,
                "body": redact(body),  # redacao na fronteira, sempre
                "captured_at": datetime.now(UTC),
            }
        )

    def last_workflow_snapshot(self, workflow_id: str) -> dict[str, Any] | None:
        historico = self.snapshots.get(workflow_id)
        return historico[-1] if historico else None

    def has_workflow_snapshots(self) -> bool:
        return bool(self.snapshots)

    def list_resources(self, source: str, kind: str) -> list[dict[str, Any]]:
        return [r for (s, k, _), r in self.resources.items() if s == source and k == kind]

    def upsert_resource(self, **kwargs: Any) -> None:
        key = (kwargs["source"], kwargs["kind"], kwargs["external_id"])
        now = datetime.now(UTC)
        existing = self.resources.get(key)
        self.resources[key] = {
            **kwargs,
            "first_seen": existing["first_seen"] if existing else now,
            "last_seen": now,
            "missing_since": None,
        }

    def record_reconciliation(self, **kwargs: Any) -> None:
        self.reconciliations.append(kwargs)

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
        self.container_observations.setdefault(container_id, []).append(
            {
                "ts": at,
                "tenant_id": tenant_id,
                "state": state,
                "health": health,
                "restart_count": restart_count,
                "mem_pct": mem_pct,
                "cpu_pct": cpu_pct,
            }
        )

    def last_container_observation(self, container_id: str) -> dict[str, Any] | None:
        historico = self.container_observations.get(container_id)
        return historico[-1] if historico else None
