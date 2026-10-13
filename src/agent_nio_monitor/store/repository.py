"""Contrato de persistencia + implementacao em memoria.

O pipeline depende deste protocolo, nao do psycopg. Isso existe por um motivo
concreto: o teste de integracao precisa rodar sem Postgres, senao a suite so
verifica algo em dia em que o banco esta de pe -- e nao verifica nada no dia em
que o banco esta fora.

`InMemoryRepository` nao e mock: implementa a mesma semantica (dedup por id,
streak, cursor, redacao) e por isso exercita o pipeline de verdade.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..redaction import redact


@dataclass
class ExecutionRecord:
    id: int
    started_at: datetime
    workflow_id: str
    status: str
    stopped_at: datetime | None = None
    mode: str | None = None
    tenant_id: str | None = None
    workflow_name: str | None = None

    @property
    def duration_ms(self) -> int | None:
        """Derivado de stoppedAt - startedAt (o summary da API nao traz duracao).

        Clamp em 0: com clock skew entre o n8n e o host, `stopped_at < started_at`
        produzia -3600000 ms, que ia para o banco e envenenava qualquer p95 ou
        media. Medido em producao antes da correcao. A migration 002 tambem
        rejeita negativo no banco -- defesa em profundidade.
        """
        if self.stopped_at is None:
            return None
        bruto = (self.stopped_at - self.started_at).total_seconds() * 1000
        return max(0, int(bruto))


@dataclass
class FailureRecord:
    execution_id: int
    started_at: datetime
    workflow_id: str
    error_msg: str | None = None
    last_node: str | None = None
    run_data: Any = None
    truncated: bool = False


@dataclass
class Streak:
    """Contraste entre `workflow_streak` no Postgres e `InMemoryRepository`.

    `tenant_id` e `workflow_name` sao campos REAIS, nao atributos injetados.
    Antes eles eram setados por `s.tenant_id = ...` com `# type: ignore`, o que
    funcionava na InMemory (mesmo objeto) e nao existia no objeto devolvido por
    `PostgresRepository.get_streak` -- que reconstruia um `Streak` limpo. O
    resultado era nome de workflow perdido em producao e presente no teste.
    """

    workflow_id: str
    count: int
    first_at: datetime | None
    last_at: datetime | None
    tenant_id: str | None = None
    workflow_name: str | None = None
    last_notified_at: datetime | None = None
    resolved_at: datetime | None = None


@dataclass
class Change:
    kind: str
    path: str
    before: Any = None
    after: Any = None


@dataclass
class EventRecord:
    id: uuid.UUID
    ts: datetime
    source: str
    event_type: str
    severity: str
    title: str
    tenant_id: str | None = None
    resource_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    streak_count: int = 1
    changes: list[Change] = field(default_factory=list)


@dataclass
class CursorState:
    source: str
    scope: str
    cursor: str | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0


class Repository(ABC):
    @abstractmethod
    def get_cursor(self, source: str, scope: str) -> CursorState | None: ...

    @abstractmethod
    def set_cursor(self, state: CursorState) -> None: ...

    @abstractmethod
    def record_executions(self, records: list[ExecutionRecord]) -> int:
        """Retorna quantos foram realmente novos (dedup por id)."""

    @abstractmethod
    def record_failure(self, failure: FailureRecord) -> None: ...

    @abstractmethod
    def execution_exists(self, execution_id: int) -> bool: ...

    @abstractmethod
    def bump_streak(self, workflow_id: str, at: datetime, tenant_id: str | None, name: str | None) -> Streak: ...

    @abstractmethod
    def get_streak(self, workflow_id: str) -> Streak | None: ...

    @abstractmethod
    def mark_streak_notified(self, workflow_id: str, at: datetime) -> None: ...

    @abstractmethod
    def resolve_streak(self, workflow_id: str, at: datetime) -> None: ...

    @abstractmethod
    def save_event(self, event: EventRecord) -> None: ...

    @abstractmethod
    def save_notification(self, **kwargs: Any) -> None: ...

    @abstractmethod
    def save_capabilities(self, rows: list[dict[str, Any]]) -> None: ...

    @abstractmethod
    def save_workflow_snapshot(self, workflow_id: str, version_id: str | None, active: bool, body: Any) -> None: ...

    @abstractmethod
    def last_workflow_snapshot(self, workflow_id: str) -> dict[str, Any] | None:
        """Snapshot mais recente. E a base de comparacao do diff estrutural (T5):
        sem ele, cada ciclo compararia contra nada e nunca detectaria mudanca."""

    @abstractmethod
    def has_workflow_snapshots(self) -> bool:
        """Ja existe linha de base? Sem ela, workflow sem snapshot e 'primeira
        coleta'; com ela, e workflow NOVO."""

    @abstractmethod
    def upsert_resource(self, **kwargs: Any) -> None: ...

    @abstractmethod
    def list_resources(self, source: str, kind: str) -> list[dict[str, Any]]:
        """Recursos conhecidos de um tipo (external_id, name, meta). Base de
        comparacao da stack: o snapshot do compose mora em `meta`."""

    @abstractmethod
    def record_reconciliation(self, **kwargs: Any) -> None: ...

    @abstractmethod
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
        """Uma linha por container por ciclo.

        A tabela `container_observation` existia no schema desde a 001 sem NINGUEM
        escrever nela -- e sem dado nao existe deteccao de restart-loop nem
        serie temporal de consumo.
        """

    @abstractmethod
    def last_container_observation(self, container_id: str) -> dict[str, Any] | None:
        """Observacao mais recente. E o que permite comparar estado e detectar
        mudanca -- e restart-loop precisa de historico, nao so do estado atual."""


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

        O Postgres ja fazia `count = count + 1` incondicional. Esta metodo agora
        e a mesma semantica -- e `tests/unit/test_parity.py` trava a equivalencia.
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


def now_utc() -> datetime:
    return datetime.now(UTC)


def new_event_id() -> uuid.UUID:
    return uuid.uuid4()