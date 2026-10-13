"""Heartbeat e deteccao de buraco.

Pre-requisito estrutural de T7: um monitor cujo modo de falha e SILENCIO precisa
que ALGUEM de fora note. Sem isso, "o Hermes parou" e indistinguivel de "o
Hermes nao achou nada".

O endpoint e minimo de proposito: nao e um dashboard, e uma prova de vida que um
cron em outra maquina consulta. Nada de autenticacao porque nao ha dado sensivel
-- e expor dado sensivel aqui criaria um problema novo para resolver um antigo.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from aiohttp import web

log = structlog.get_logger(__name__)

# AppKey em vez de string: o aiohttp emite aviso ao usar chave string em
# `Application[...]`, e a chave tipada impede colisao silenciosa.
HEALTH_KEY: web.AppKey[HealthState] = web.AppKey("health")
MAX_STALE_KEY: web.AppKey[timedelta] = web.AppKey("max_stale")


@dataclass
class HealthState:
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_collect_at: dict[str, datetime] = field(default_factory=dict)
    collect_errors: dict[str, int] = field(default_factory=dict)
    n8n_up: bool | None = None
    gaps_detected: int = 0
    queue: dict[str, Any] = field(default_factory=dict)

    def mark(self, source: str, ok: bool) -> None:
        if ok:
            self.last_collect_at[source] = datetime.now(UTC)
            self.collect_errors[source] = 0
        else:
            self.collect_errors[source] = self.collect_errors.get(source, 0) + 1

    def stale_sources(self, max_age: timedelta) -> list[str]:
        agora = datetime.now(UTC)
        return [s for s, t in self.last_collect_at.items() if agora - t > max_age]

    def snapshot(self, max_stale: timedelta) -> dict[str, Any]:
        agora = datetime.now(UTC)
        return {
            "status": "ok",
            "uptime_seconds": int((agora - self.started_at).total_seconds()),
            "heartbeat_at": agora.isoformat(),
            "n8n_up": self.n8n_up,
            "last_collect": {k: v.isoformat() for k, v in self.last_collect_at.items()},
            "collect_errors": dict(self.collect_errors),
            "stale_sources": self.stale_sources(max_stale),
            "gaps_detected": self.gaps_detected,
            "queue": dict(self.queue),
        }


async def _health(request: web.Request) -> web.Response:
    state: HealthState = request.app[HEALTH_KEY]
    max_stale: timedelta = request.app[MAX_STALE_KEY]
    snap = state.snapshot(max_stale)
    # 503 quando ha fonte parada demais: o cron externo distingue "vivo" de
    # "vivo mas nao coletando", que sao falhas diferentes.
    status = 503 if snap["stale_sources"] or state.n8n_up is False else 200
    return web.json_response(snap, status=status)


async def _ready(request: web.Request) -> web.Response:
    return web.json_response({"ready": True})


class HealthServer:
    def __init__(self, state: HealthState, *, host: str = "0.0.0.0", port: int = 8099,
                 max_stale: timedelta = timedelta(minutes=10)) -> None:
        self.state = state
        self.host = host
        self.port = port
        self.max_stale = max_stale
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app[HEALTH_KEY] = self.state
        app[MAX_STALE_KEY] = self.max_stale
        app.router.add_get("/healthz", _health)
        app.router.add_get("/healthz/ready", _ready)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("health_server_ouvindo", host=self.host, port=self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None


def deadman_curl_example(url: str, *, stale_after: int = 900) -> str:
    """Comando pronto pro runbook. FALHA de proposito quando o endpoint nao
    responde em 900 s -- e isso que o cron externo transforma em alerta."""
    return (
        f"curl -fsS --max-time 10 {url} "
        f"-o /dev/null || echo 'HERMES-MORTO: sem heartbeat ha mais de {stale_after}s'"
    )


def log_linha_alerta(payload: dict[str, Any]) -> str:
    """Linha de log para consumo por shipper. Prefixo fixo para alerta e grep."""
    return json.dumps({"prefix": "HERMES", "kind": "alerta", **payload}, ensure_ascii=False)