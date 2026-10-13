"""Pipeline principal. Cada estagio e isolado e testavel; `main.py` so monta o
CLI e o scheduler em torno desta classe.

Ordem deliberada: Capabilities (D2) -> health sobe -> coleta -> regra -> texto ->
envio -> registro. Saude ANTES da coleta para que o cron externo veja o processo
desde o primeiro segundo.
"""

from __future__ import annotations

from typing import Any

import structlog

from .config.loader import ConfigError, load_policy
from .config.schema import AppConfig
from .health import HealthState
from .llm.analyst import AlertAnalyst
from .llm.client import LlmClient
from .notify.compose import compor
from .notify.mailer import DispatchResult, Notifier
from .notify.pipeline import EventPipeline
from .notify.resolution import ResolutionChecker
from .notify.people import exclusive_by_workflow, people_by_project, portainer_users
from .notify.resolver import RecipientResolver
from .notify.templates import formatar_horas
from .rules.engine import RuleEngine
from .sources.n8n.client import N8NClient
from .sources.n8n.collector import ExecutionErrorCollector
from .sources.n8n.workflow_index import WorkflowIndex
from .sources.n8n.workflow_monitor import WorkflowMonitor
from .sources.portainer.suite import build_portainer
from .store.postgres_repository import PostgresRepository
from .store.queue import InMemoryQueue, NotificationQueue, PostgresQueue
from .store.repository import EventRecord, Repository, new_event_id, now_utc

log = structlog.get_logger()


class Agent:
    def __init__(self, cfg: AppConfig, repo: Repository, queue: NotificationQueue | None = None) -> None:
        self.cfg = cfg
        self.repo = repo
        self.policy = load_policy()
        self.rules = RuleEngine(self.policy)
        self.health = HealthState()
        self.llm = LlmClient(
            cfg.llm.base_url, cfg.llm.model,
            timeout=cfg.llm.timeout_seconds,
            temperature=cfg.llm.temperature,
            max_tokens=cfg.llm.max_tokens,
        )
        self.analyst = AlertAnalyst(self.llm, enabled=cfg.llm.fallback_to_template)
        self.notifier = Notifier(
            webhook_url=f"{cfg.mail.webhook_url}{cfg.mail.webhook_path}",
            timeout=cfg.mail.timeout_seconds,
            max_attempts=cfg.mail.max_attempts,
            verify_ssl=cfg.active.n8n.verify_ssl,
        )
        self.resolver = RecipientResolver(
            N8NClient(
                _active_url(cfg),
                _secret(cfg),
                verify_ssl=cfg.active.n8n.verify_ssl,
                timeout=cfg.active.n8n.timeout_seconds,
            ),
            people=people_by_project(),
            usuarios=portainer_users(),
            exclusivos=exclusive_by_workflow(),
            max_recipients=self.policy.max_recipients,
        )
        self.n8n = self.resolver.client
        self.index = WorkflowIndex(self.n8n)
        self.workflow_monitor = WorkflowMonitor(self.n8n, self.repo)
        self.containers, self.stacks, self.resources = build_portainer(cfg, self.repo) or (None, None, None)
        self.pipeline = EventPipeline(
            repo=self.repo, queue=queue if queue is not None else InMemoryQueue(), rules=self.rules,
            polling=cfg.polling, health=self.health, dispatch=self.dispatch,
            checker=ResolutionChecker(
                containers=self.containers, resources=self.resources, n8n=self.n8n, repo=self.repo,
                limites=cfg.policy.thresholds,
            ),
        )

    # ------------------------------------------------------------ ciclo n8n

    async def collect_execution_errors(self) -> list[EventRecord]:
        collector = ExecutionErrorCollector(self.n8n, self.repo)
        await self.index.load()
        resultado = await collector.run(name_by_id=self.index.names)
        self.health.mark("n8n_executions", resultado.ok)
        if resultado.gap:
            self.health.gaps_detected += 1

        if not resultado.collected:
            return []

        # O streak ja foi acumulado pelo coletor. Aqui so se DECIDE o que fazer.
        eventos: list[EventRecord] = []
        ultimos = resultado.extra.get("ultimos_erros", {})
        for workflow_id, streak in resultado.extra.get("streaks", {}).items():
            falha = ultimos.get(workflow_id)
            nome = getattr(streak, "workflow_name", None) or workflow_id
            decisao = self.rules.evaluate(
                event_type="execution_failed",
                streak_count=streak.count,
                last_notified_at=streak.last_notified_at,
                now=now_utc(),
            )
            primeiro = formatar_horas(streak.first_at)
            ultimo = formatar_horas(streak.last_at)
            eventos.append(
                EventRecord(
                    id=new_event_id(),
                    ts=now_utc(),
                    source="n8n",
                    event_type="execution_failed",
                    severity=decisao.severity,
                    title=f"{nome} falhou {streak.count}x",
                    tenant_id=self.index.projects.get(workflow_id),
                    resource_id=workflow_id,
                    streak_count=streak.count,
                    detail={
                        "workflow_id": workflow_id,
                        "workflow_name": nome,
                        "workflow_nome": nome,
                        "ocorrencias": streak.count,
                        "ultima_falha_ts": streak.last_at.isoformat(),
                        "autor": self.index.authors.get(workflow_id),
                        "erro": falha.error_msg if falha else None,
                        "no": falha.last_node if falha else None,
                        "primeira": primeiro,
                        "ultima": ultimo,
                        "notificar": decisao.notify,
                        "motivo_supressao": decisao.suppress_reason,
                        "template": decisao.template,
                    },
                )
            )
        return eventos

    # -------------------------------------------------------- pipeline de alerta

    async def dispatch(self, evento: EventRecord) -> DispatchResult:
        decisao_texto = evento.detail.get("notificar", True)
        if not decisao_texto:
            log.info(
                "evento_registrado_sem_notificar",
                evento=evento.title,
                motivo=evento.detail.get("motivo_supressao"),
            )
            return DispatchResult(status="suppressed", channel="nenhuma")

        projeto = evento.tenant_id
        destinatarios = await self.resolver.resolve_for_event(projeto, evento.detail)
        if destinatarios.source in ("people_yaml", "global_fallback", "nenhum"):
            log.warning(
                "destinatario_por_fallback",
                fonte=destinatarios.source,
                detalhe=destinatarios.detail,
            )

        contexto = {
            **evento.detail, "tipo": evento.event_type,
            "titulo": evento.title, "severidade": evento.severity,
        }
        mensagem = await compor(self.analyst, evento, contexto, self.cfg)

        resultado = await self.notifier.send(
            recipients=destinatarios.emails,
            subject=mensagem.assunto,
            body=mensagem.corpo,
            html=mensagem.html,
            n8n_up=bool(self.health.n8n_up is not False),
            severity=evento.severity,
            metadata={
                "event_id": str(evento.id),
                "streak": evento.streak_count,
                "llm": mensagem.status_llm,
            },
        )

        self.repo.save_notification(
            event_id=evento.id,
            channel=resultado.channel,
            recipients=resultado.recipients,
            subject=mensagem.assunto,
            body=mensagem.corpo,
            status=resultado.status,
            error=resultado.error,
            attempts=resultado.attempts,
        )
        if resultado.status in ("sent", "sent_to_log") and evento.resource_id:
            self.repo.mark_streak_notified(evento.resource_id, now_utc())
        return resultado

    async def run_once(self) -> dict[str, Any]:
        inicio = now_utc()
        eventos = await self.collect_execution_errors()
        enfileirados = await self.process_events(eventos)
        return {
            "eventos": len(eventos),
            "enfileirados": enfileirados,
            "ms": int((now_utc() - inicio).total_seconds() * 1000),
        }

    async def process_events(self, eventos: list[EventRecord]) -> int:
        """Grava e enfileira. Quem envia e a fila (`flush_queue`), que antes reconfere
        se o problema continua -- um unico lugar para todos os coletores."""
        return self.pipeline.enqueue(eventos)

    async def flush_queue(self) -> dict[str, int]:
        return await self.pipeline.flush()

    # --------------------------------------------------------- ciclos T5/T6

    def _decidir(self, evento: EventRecord) -> EventRecord:
        """Aplica a politica unica (RuleEngine) num evento ja construido pelo
        coletor. Mesma logica que `collect_execution_errors` aplica inline --
        aqui centralizada porque workflow/container nao tem streak proprio."""
        decisao = self.rules.evaluate(event_type=evento.event_type, now=evento.ts)
        evento.severity = decisao.severity
        evento.detail.setdefault("autor", self.index.authors.get(evento.resource_id or ""))
        evento.tenant_id = evento.tenant_id or self.index.projects.get(evento.resource_id or "")
        evento.detail["notificar"] = decisao.notify
        evento.detail["motivo_supressao"] = decisao.suppress_reason
        evento.detail["template"] = decisao.template
        return evento

    async def _ciclo(self, resultado, chave: str) -> int:  # noqa: ANN001 - CollectorResult
        self.health.mark(chave, resultado.ok)
        eventos = [self._decidir(e) for e in resultado.extra.get("eventos", [])]
        return await self.process_events(eventos)

    async def check_workflows_full(self) -> int:
        await self.index.load()
        resultado = await self.workflow_monitor.check_structural_diff(name_by_id=self.index.names)
        return await self._ciclo(resultado, "n8n_workflows_full")

    async def check_containers(self) -> int:
        if self.containers is None:
            return 0
        return await self._ciclo(await self.containers.check_state(), "portainer_containers")

    async def check_stacks(self) -> int:
        if self.stacks is None:
            return 0
        return await self._ciclo(await self.stacks.check_changes(), "portainer_stacks")

    async def check_container_resources(self) -> int:
        if self.resources is None:
            return 0
        return await self._ciclo(await self.resources.check_resources(), "portainer_resources")


def _active_url(cfg: AppConfig) -> str:
    return cfg.active.n8n.url


def _secret(cfg: AppConfig) -> str:
    import os

    return os.environ.get(cfg.active.n8n.api_key_env, "")


def build(cfg: AppConfig, repo: Repository | None = None) -> Agent:
    if repo is None:
        db = _make_db(cfg)
        db.open()
        db.migrate()
        db.ensure_partitions()
        return Agent(cfg, PostgresRepository(db), PostgresQueue(db))
    return Agent(cfg, repo)


def _make_db(cfg: AppConfig):  # noqa: ANN202
    from .store.db import Database

    dsn = cfg.database.dsn.replace("${DATABASE_DSN}", "")
    if not dsn:
        raise ConfigError("DATABASE_DSN ausente (ver .env.example)")
    return Database(
        dsn, cfg.database.db_schema, cfg.database.pool_min, cfg.database.pool_max
    )
