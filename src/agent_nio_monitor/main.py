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

from .agent import Agent
from .bootstrap import build
from .capabilities import CapabilityReport, detect, fontes_do_config
from .capabilities_report import capability_rows, render_table
from .config.loader import ConfigError, load
from .config.schema import AppConfig
from .fuso import FUSO_NOME
from .health import HealthServer
from .sources.powerautomate.probe import sondar_do_config

__all__ = ["Agent", "build", "main"]

log = structlog.get_logger()

# Particoes: 6 h deixa quatro chances por dia antes da virada do mes.
MANUTENCAO_SEGUNDOS = 6 * 3600


def local_timestamper(timezone: str = FUSO_NOME):  # noqa: ANN201 - processor do structlog
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

    if cfg.active.powerautomate is not None:
        print("\n=== POWER AUTOMATE ===")
        print(await sondar_do_config(cfg.active.powerautomate))

    sources = fontes_do_config(cfg)
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
        if agent.desktop is not None:
            await agent.desktop.client.aclose()
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
    """Coleta e disparo entre 25 s e 1 min: erros e containers 30 s; workflows (diff,
    ativacao e criacao), stacks e consumo 60 s; a fila de disparo reconfere a cada 10 s.

    Cada job tem o PROPRIO laco: o ciclo lento de workflows (leitura de 11 MB) nao atrasa
    os de 30 s, e o mesmo job nunca roda sobreposto. Deliberadamente NAO usa APScheduler:
    timers com asyncio bastam e nao precisamos de job em disco.

    A sonda de capacidade (D2) roda uma vez, em task propria. Ela vem DEPOIS do
    health subir por um motivo concreto: sondar antes bloquearia o `/healthz`
    durante a sonda, e o cron externo do runbook veria ausencia de resposta em
    vez de "vivo". Task separada, e nao `await` na frente do gather, para que a
    primeira coleta nao espere a sonda terminar.
    """
    p = agent.cfg.polling
    jobs = {
        "executions": (p.execution_errors_seconds, agent.run_once),
        "workflows_full": (p.workflow_full_seconds, agent.check_workflows_full),
        "credentials": (p.workflow_full_seconds, agent.check_credentials),
        "containers_state": (p.container_state_seconds, agent.check_containers),
        "stacks": (p.stack_changes_seconds, agent.check_stacks),
        "containers_stats": (p.container_stats_seconds, agent.check_container_resources),
        "power_automate": (p.execution_errors_seconds, agent.check_power_automate),
        "execution_paths": (p.execution_errors_seconds, agent.check_execution_paths),
        "container_logs": (p.container_state_seconds, agent.check_container_logs),
        "bubble": (p.bubble_poll_seconds, agent.check_bubble),
        "fila": (p.queue_seconds, agent.flush_queue),
        "particoes": (MANUTENCAO_SEGUNDOS, agent.maintain_partitions),
    }
    await asyncio.gather(
        *(_laco_do_job(nome, intervalo, funcao, stop) for nome, (intervalo, funcao) in jobs.items()),
        asyncio.create_task(agent.run_capabilities()),
    )


async def _laco_do_job(nome: str, intervalo: float, funcao, stop: asyncio.Event) -> None:  # noqa: ANN001
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        inicio = loop.time()
        try:
            await funcao()
        except Exception as exc:  # noqa: BLE001 - um job nao derruba o agente
            log.error("job_falhou", job=nome, erro=str(exc))
        duracao = loop.time() - inicio
        log.debug("ciclo_duracao_ms", job=nome, ms=int(duracao * 1000))
        if duracao > intervalo:
            log.warning("ciclo_mais_lento_que_o_intervalo", job=nome, ms=int(duracao * 1000), intervalo_s=intervalo)
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(intervalo - duracao, 0))
        except TimeoutError:
            continue


if __name__ == "__main__":
    raise SystemExit(main())
