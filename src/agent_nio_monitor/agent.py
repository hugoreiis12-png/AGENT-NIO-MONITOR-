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
from .llm.writer import AlertWriter, LlmClient
from .notify.mailer import DispatchResult, Notifier
from .notify.resolver import RecipientResolver
from .notify.templates import formatar_horas, render
from .rules.engine import RuleEngine
from .sources.n8n.client import N8NClient
from .sources.n8n.collector import ExecutionErrorCollector
from .sources.n8n.workflow_index import WorkflowIndex
from .sources.n8n.workflow_monitor import WorkflowMonitor
from .sources.portainer.client import PortainerClient
from .sources.portainer.collector import ContainerCollector
from .store.postgres_repository import PostgresRepository
from .store.repository import EventRecord, Repository, new_event_id, now_utc

log = structlog.get_logger()


class Agent:
    def __init__(self, cfg: AppConfig, repo: Repository) -> None:
        self.cfg = cfg
        self.repo = repo
        self.policy = load_policy()
        self.rules = RuleEngine(self.policy)
        self.health = HealthState()
        self.llm = LlmClient(
            cfg.llm.base_url, cfg.llm.model,
            timeout=cfg.llm.timeout_seconds,
            num_ctx=cfg.llm.num_ctx,
            temperature=cfg.llm.temperature,
            max_tokens=cfg.llm.max_tokens,
        )
        self.writer = AlertWriter(self.llm, enabled=cfg.llm.fallback_to_template)
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
            people=_people(),
            max_recipients=self.policy.max_recipients,
        )
        self.n8n = self.resolver.client
        self.index = WorkflowIndex(self.n8n)
        self.workflow_monitor = WorkflowMonitor(self.n8n, self.repo)
        self.containers = _build_container_collector(cfg, self.repo)

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
        for workflow_id, streak in resultado.extra.get("streaks", {}).items():
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
        destinatarios = await self.resolver.resolve_for_project(projeto)
        if destinatarios.source in ("people_yaml", "global_fallback", "nenhum"):
            log.warning(
                "destinatario_por_fallback",
                fonte=destinatarios.source,
                detalhe=destinatarios.detail,
            )

        contexto = dict(evento.detail)
        abertura, status_llm = await self.writer.write(contexto)
        assunto, corpo = render(
            evento.detail.get("template", "generic"), event=evento,
            contexto={**contexto, "abertura": abertura},
        )
        corpo = f"{abertura}\n{corpo}"

        resultado = await self.notifier.send(
            recipients=destinatarios.emails,
            subject=assunto,
            body=corpo,
            n8n_up=bool(self.health.n8n_up is not False),
            severity=evento.severity,
            metadata={
                "event_id": str(evento.id),
                "streak": evento.streak_count,
                "llm": status_llm,
            },
        )

        self.repo.save_notification(
            event_id=evento.id,
            channel=resultado.channel,
            recipients=resultado.recipients,
            subject=assunto,
            body=corpo,
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
        enviados = await self.process_events(eventos)
        return {
            "eventos": len(eventos),
            "enviados": enviados,
            "ms": int((now_utc() - inicio).total_seconds() * 1000),
        }

    async def process_events(self, eventos: list[EventRecord]) -> int:
        """Salva e despacha. Compartilhado por `run_once` e pelos ciclos T5/T6
        -- um unico lugar decide o que acontece depois que um evento nasce."""
        enviados = 0
        for e in eventos:
            self.repo.save_event(e)
            r = await self.dispatch(e)
            if r.status in ("sent", "sent_to_log"):
                enviados += 1
        return enviados

    # --------------------------------------------------------- ciclos T5/T6

    def _decidir(self, evento: EventRecord) -> EventRecord:
        """Aplica a politica unica (RuleEngine) num evento ja construido pelo
        coletor. Mesma logica que `collect_execution_errors` aplica inline --
        aqui centralizada porque workflow/container nao tem streak proprio."""
        decisao = self.rules.evaluate(event_type=evento.event_type, now=evento.ts)
        evento.severity = decisao.severity
        evento.tenant_id = evento.tenant_id or self.index.projects.get(evento.resource_id or "")
        evento.detail["notificar"] = decisao.notify
        evento.detail["motivo_supressao"] = decisao.suppress_reason
        evento.detail["template"] = decisao.template
        return evento

    async def check_workflows_active(self) -> int:
        await self.index.load()
        resultado = await self.workflow_monitor.check_active_state(name_by_id=self.index.names)
        self.health.mark("n8n_workflows_state", resultado.ok)
        eventos = [self._decidir(e) for e in resultado.extra.get("eventos", [])]
        return await self.process_events(eventos)

    async def check_workflows_full(self) -> int:
        await self.index.load()
        resultado = await self.workflow_monitor.check_structural_diff(name_by_id=self.index.names)
        self.health.mark("n8n_workflows_full", resultado.ok)
        eventos = [self._decidir(e) for e in resultado.extra.get("eventos", [])]
        return await self.process_events(eventos)

    async def check_containers(self) -> int:
        if self.containers is None:
            return 0
        resultado = await self.containers.check_state()
        self.health.mark("portainer_containers", resultado.ok)
        eventos = [self._decidir(e) for e in resultado.extra.get("eventos", [])]
        return await self.process_events(eventos)


def _active_url(cfg: AppConfig) -> str:
    return cfg.active.n8n.url


def _secret(cfg: AppConfig) -> str:
    import os

    return os.environ.get(cfg.active.n8n.api_key_env, "")


def _build_container_collector(cfg: AppConfig, repo: Repository) -> ContainerCollector | None:
    """None quando Portainer nao esta configurado ou sem credencial -- mesmo
    criterio do `doctor`. Sem isso o agente cairia na borda errada: travar o
    boot por falta de um recurso opcional, em vez de so deixar o ciclo de lado."""
    import os

    env = cfg.active.portainer
    if env is None:
        return None
    api_key = os.environ.get(env.api_key_env, "")
    if not api_key:
        log.warning("portainer_sem_credencial_ciclo_de_container_desligado")
        return None
    client = PortainerClient(env.url, api_key, verify_ssl=env.verify_ssl, timeout=env.timeout_seconds)
    return ContainerCollector(client, repo)


def _people() -> dict[str, list[str]]:
    from pathlib import Path

    import yaml

    path = Path(__file__).resolve().parents[2] / "config" / "people.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")).get("projects", {})


def build(cfg: AppConfig, repo: Repository | None = None) -> Agent:
    if repo is None:
        db = _make_db(cfg)
        db.open()
        db.ensure_partitions()
        repo = PostgresRepository(db)
    return Agent(cfg, repo)


def _make_db(cfg: AppConfig):  # noqa: ANN202
    from .store.db import Database

    dsn = cfg.database.dsn.replace("${DATABASE_DSN}", "")
    if not dsn:
        raise ConfigError("DATABASE_DSN ausente (ver .env.example)")
    return Database(
        dsn, cfg.database.db_schema, cfg.database.pool_min, cfg.database.pool_max
    )
