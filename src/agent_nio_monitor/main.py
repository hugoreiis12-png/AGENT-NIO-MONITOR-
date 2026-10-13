"""Pipeline principal. Cada estagio e isolado e testavel; o scheduler apenas
repetem o que a CLI roda uma vez.

Ordem deliberada:Capabilities (D2) -> health sobe -> coleta -> regra -> texto ->
envio -> registro. Saude ANTES da coleta para que o cron externo veja o processo
desde o primeiro segundo.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from typing import Any

import structlog

from .capabilities import CapabilityReport, capability_rows, detect, render_table
from .config.loader import ConfigError, load, load_policy
from .config.schema import AppConfig
from .health import HealthServer, HealthState
from .llm.writer import AlertWriter, LlmClient
from .notify.mailer import DispatchResult, Notifier
from .notify.resolver import RecipientResolver
from .notify.templates import formatar_horas, render
from .rules.engine import RuleEngine
from .sources.n8n.client import N8NClient
from .sources.n8n.collector import ExecutionErrorCollector
from .store.postgres_repository import PostgresRepository
from .store.repository import EventRecord, Repository, new_event_id, now_utc

log = structlog.get_logger()


def setup_logging(level: str) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(__import__("logging"), level.upper(), 20)
        ),
        cache_logger_on_first_use=True,
    )


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

    # ------------------------------------------------------------ ciclo n8n

    async def collect_execution_errors(self) -> list[EventRecord]:
        collector = ExecutionErrorCollector(self.n8n, self.repo)
        nomes = await self._workflow_names()
        resultado = await collector.run(name_by_id=nomes)
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
                    tenant_id=getattr(streak, "tenant_id", None),
                    resource_id=workflow_id,
                    streak_count=streak.count,
                    detail={
                        "workflow_id": workflow_id,
                        "workflow_name": nome,
                        "ocorrencias": streak.count,
                        "primeira": primeiro,
                        "ultima": ultimo,
                        "notificar": decisao.notify,
                        "motivo_supressao": decisao.suppress_reason,
                    },
                )
            )
        return eventos

    async def _workflow_names(self) -> dict[str, str]:
        cache = getattr(self, "_names_cache", None)
        if cache is None:
            nomes: dict[str, str] = {}
            try:
                async for wf in self.n8n.iter_workflows():
                    nomes[str(wf.get("id"))] = str(wf.get("name") or wf.get("id"))
            except Exception as exc:  # noqa: BLE001 - nome e opcional, coleta nao pode morrer por isso
                log.warning("nomes_de_workflow_indisponiveis", erro=str(exc))
            self._names_cache = nomes
        return self._names_cache

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
            _template_para(evento), event=evento,
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
            recipients=resultado.emails,
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
        enviados = 0
        for e in eventos:
            self.repo.save_event(e)
            r = await self.dispatch(e)
            if r.status in ("sent", "sent_to_log"):
                enviados += 1
        return {
            "eventos": len(eventos),
            "enviados": enviados,
            "ms": int((now_utc() - inicio).total_seconds() * 1000),
        }


def _template_para(evento: EventRecord) -> str:
    return "workflow_failing" if evento.streak_count > 1 else "execution_failed"


def _active_url(cfg: AppConfig) -> str:
    return cfg.active.n8n.url


def _secret(cfg: AppConfig) -> str:
    import os

    return os.environ.get(cfg.active.n8n.api_key_env, "")


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


async def cmd_doctor(cfg: AppConfig) -> int:
    """D2 em acao: sonda e imprime. E a resposta definitiva sobre a versao."""
    import os

    env = cfg.active
    sources: dict[str, dict[str, str]] = {}

    if os.environ.get(env.n8n.api_key_env):
        sources["n8n"] = {
            "url": env.n8n.url,
            "api_key": os.environ[env.n8n.api_key_env],
        }
    if env.portainer and os.environ.get(env.portainer.api_key_env):
        sources["portainer"] = {
            "url": os.environ.get("PORTAINER_URL", env.portainer.url),
            "api_key": os.environ[env.portainer.api_key_env],
        }
    if not sources:
        print("Nenhuma credencial encontrada. Verifique o .env.")
        return 2

    reports: dict[str, CapabilityReport] = await detect(sources)
    for nome, rep in reports.items():
        print(f"\n=== {nome.upper()} ===")
        print(render_table(rep))
        rows = capability_rows(rep)
        for r in rows:
            log.debug("capability", **r)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="agent-nio-monitor")
    parser.add_argument("comando", choices=["run", "once", "doctor"], nargs="?", default="run")
    args = parser.parse_args()

    try:
        cfg = load()
    except ConfigError as exc:
        print(f"ERRO DE CONFIG: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg.log_level)

    if args.comando == "doctor":
        return asyncio.run(cmd_doctor(cfg))

    agent = build(cfg)
    if args.comando == "once":
        print(asyncio.run(agent.run_once()))
        return 0

    async def _run() -> None:
        servidor = HealthServer(agent.health)
        await servidor.start()
        stop = asyncio.Event()
        _instala_handlers_de_sinal(stop)
        log.info("agent_nio_monitor_iniciado", health_port=8099)
        relogio = asyncio.create_task(_agenda(agent, stop))
        await stop.wait()
        relogio.cancel()
        await servidor.stop()
        await agent.llm.aclose()
        await agent.notifier.aclose()
        await agent.n8n.aclose()
        log.info("agent_encerrado")

    asyncio.run(_run())
    return 0


def _instala_handlers_de_sinal(stop: asyncio.Event) -> None:
    """SIGINT/SIGTERM -> shutdown limpo.

    `loop.add_signal_handler` levanta NotImplementedError no Windows
    (ProactorEventLoop). Verificado executando em Windows: sem esta guarda,
    `python -m agent_nio_monitor.main run` morre no boot. O agente roda em
    servidor Linux, mas o desenvolvimento e em Windows, e crash de boot e o pior
    jeito de descobrir isso.

    No Windows o Ctrl+C continua funcionando pelo KeyboardInterrupt do asyncio,
    entao a perda do handler e degradacao, nao impedimento.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError, ValueError) as exc:
            log.warning(
                "handler_de_sinal_indisponivel",
                sinal=getattr(sig, "name", str(sig)),
                erro=type(exc).__name__,
                efeito="Ctrl+C continua funcionando; SIGTERM nao encerra o processo",
            )


async def _agenda(agent: Agent, stop: asyncio.Event) -> None:
    """Cadencias do plano: erro 60s, estado 300s, corpo 900s.

    Deliberadamente NAO usa APScheduler ainda: quatro timers com asyncio bastam,
    e APScheduler traria job em disco (persistencia) que nao precisamos. Se
    aparecer janela de manutenção, ai sim.
    """
    intervals = {
        "executions": agent.cfg.polling.execution_errors_seconds,
        "workflows_state": agent.cfg.polling.workflow_active_seconds,
        "workflows_full": agent.cfg.polling.workflow_full_seconds,
        "reconcile": agent.cfg.polling.reconciliation_seconds,
    }
    proximos = {nome: 0.0 for nome in intervals}
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        agora = loop.time()
        for nome, intervalo in intervals.items():
            if agora >= proximos[nome]:
                proximos[nome] = agora + intervalo
                try:
                    if nome == "executions":
                        await agent.run_once()
                    else:
                        log.debug("job_pendente", job=nome)
                except Exception as exc:  # noqa: BLE001 - um job nao derruba o agente
                    log.error("job_falhou", job=nome, erro=str(exc))
        await asyncio.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())



