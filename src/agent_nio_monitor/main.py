"""Entrypoint da CLI: argparse, boot do health server e o scheduler.

A classe `Agent` (o pipeline em si) mora em `agent.py` -- aqui so se monta o
processo em torno dela. `Agent` e `build` ficam reexportados porque
`agent_nio_monitor.main:Agent` e contrato publico (usado por teste e script).
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import structlog

from .agent import Agent, build
from .capabilities import CapabilityReport, capability_rows, detect, render_table
from .config.loader import ConfigError, load
from .config.schema import AppConfig
from .health import HealthServer

__all__ = ["Agent", "build", "main"]

log = structlog.get_logger()

LOG_TIMEZONE = "America/Sao_Paulo"


def local_timestamper(timezone: str = LOG_TIMEZONE):  # noqa: ANN201 - processor do structlog
    """Carimbo ISO com o deslocamento explicito (ex.: -03:00): o instante continua
    inequivoco, mas o horario bate com o relogio de quem le o log."""
    tz = ZoneInfo(timezone)

    def processor(_logger: object, _name: str, event_dict: dict) -> dict:
        event_dict["timestamp"] = datetime.now(tz).isoformat(timespec="microseconds")
        return event_dict

    return processor


def setup_logging(level: str) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            local_timestamper(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(__import__("logging"), level.upper(), 20)
        ),
        cache_logger_on_first_use=True,
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
        if agent.containers is not None:
            await agent.containers.client.aclose()
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
    """Cadencias do plano: erro 60s, estado de workflow 300s, diff completo
    900s, estado de container 60s, stacks e consumo 300s.

    Deliberadamente NAO usa APScheduler ainda: timers com asyncio bastam, e
    APScheduler traria job em disco (persistencia) que nao precisamos.

    `reconcile` (3600s) fica pendente -- e um ciclo de auditoria de recursos
    (`resource` table) distinto dos coletores, nao coberto por esta fiacao.
    """
    jobs = {
        "executions": (agent.cfg.polling.execution_errors_seconds, agent.run_once),
        "workflows_state": (agent.cfg.polling.workflow_active_seconds, agent.check_workflows_active),
        "workflows_full": (agent.cfg.polling.workflow_full_seconds, agent.check_workflows_full),
        "containers_state": (agent.cfg.polling.container_state_seconds, agent.check_containers),
        "stacks": (agent.cfg.polling.stack_changes_seconds, agent.check_stacks),
        "containers_stats": (agent.cfg.polling.container_stats_seconds, agent.check_container_resources),
    }
    pendentes = {"reconcile": agent.cfg.polling.reconciliation_seconds}
    intervals = {nome: intervalo for nome, (intervalo, _) in jobs.items()} | pendentes
    proximos = {nome: 0.0 for nome in intervals}
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        agora = loop.time()
        for nome, intervalo in intervals.items():
            if agora >= proximos[nome]:
                proximos[nome] = agora + intervalo
                try:
                    if nome in jobs:
                        await jobs[nome][1]()
                    else:
                        log.debug("job_pendente", job=nome)
                except Exception as exc:  # noqa: BLE001 - um job nao derruba o agente
                    log.error("job_falhou", job=nome, erro=str(exc))
        await asyncio.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
