"""Heartbeat e deteccao de buraco.

Pre-requisito estrutural de T7: um monitor cujo modo de falha e SILENCIO precisa
que ALGUEM de fora note. Sem isso, "o Hermes parou" e indistinguivel de "o
Hermes nao achou nada".

O endpoint e minimo de proposito: nao e um dashboard, e uma prova de vida que um
cron em outra maquina consulta. Nada de autenticacao porque nao ha dado sensivel
-- e expor dado sensivel aqui criaria um problema novo para resolver um antigo.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
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

# Ciclos consecutivos de falha antes de a fonte contar como cega. Um nao basta:
# ver `fontes_cego`.
FALHAS_PARA_CEGO = 2


@dataclass
class HealthState:
    """Estado observado, escrito pelo agente e lido pelo servidor de health.

    Sao DUAS threads: o agente escreve no event loop dele, o `HealthServer`
    le num loop proprio (ver `HealthServer`). Todo acesso passa pelo `_lock`,
    senao `snapshot()` pode iterar um dict enquanto o outro lado insere -- o
    classico "dictionary changed size during iteration", e a resposta 500 que
    o dead-man's switch leria como queda.
    """

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_collect_at: dict[str, datetime] = field(default_factory=dict)
    collect_errors: dict[str, int] = field(default_factory=dict)
    n8n_up: bool | None = None
    gaps_detected: int = 0
    alertas_sem_destinatario: int = 0
    # Fonte que responde em parte. Vive separado de `collect_errors` porque o
    # significado e outro: "ok" nao significa "viu tudo".
    fontes_parciais: dict[str, Any] = field(default_factory=dict)
    queue: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def mark(self, source: str, ok: bool) -> None:
        with self._lock:
            if ok:
                self.last_collect_at[source] = datetime.now(UTC)
                self.collect_errors[source] = 0
            else:
                self.collect_errors[source] = self.collect_errors.get(source, 0) + 1

    def mark_n8n(self, ok: bool) -> None:
        """Alcancabilidade do n8n, vista pelo client -- nao por quem coleta.

        Sao coisas diferentes: `mark("n8n_executions", ok)` diz que a COLETA
        terminou bem; isto diz que o n8n ATENDEU. Uma queda do n8n que se
        manifesta so no `WorkflowIndex.load()` -- que hoje so loga warning --
        nunca passaria pelo `mark`.

        `None` ate a primeira resposta e o estado de "ainda nao se sabe", e o
        `Agent` trata como de pe: otimista no arranque, corrigido no primeiro
        ciclo (30 s).
        """
        with self._lock:
            self.n8n_up = ok

    def conta_gap(self) -> None:
        with self._lock:
            self.gaps_detected += 1

    def conta_alerta_perdido(self) -> None:
        """Alerta que entrou na fila e nao tem destinatario.

        Passa por metodo, e nao por `+=` direto no atributo: a leitura-e-escrita
        do contador acontece na thread do agente, e um `snapshot()` concorrente
        na thread do health pode ler no meio dela.
        """
        with self._lock:
            self.alertas_sem_destinatario += 1

    def registra_fonte_parcial(self, chave: str, dados: dict[str, Any]) -> None:
        with self._lock:
            self.fontes_parciais[chave] = dados

    def limpa_fonte_parcial(self, chave: str) -> None:
        with self._lock:
            self.fontes_parciais.pop(chave, None)

    def registra_fila(self, stats: dict[str, Any]) -> None:
        with self._lock:
            self.queue = stats

    def stale_sources(self, max_age: timedelta) -> list[str]:
        agora = datetime.now(UTC)
        with self._lock:
            return [s for s, t in self.last_collect_at.items() if agora - t > max_age]

    def fontes_cego(self, falhas_seguidas: int = FALHAS_PARA_CEGO) -> list[str]:
        """Fontes que falharam `falhas_seguidas` ciclos CONSECUTIVOS.

        `stale_sources` sozinho nao cobre escuridao total: ele so olha
        `last_collect_at`, que nao e atualizado justamente quando a coleta
        falha. Uma fonte que nunca chegou a ter `last_collect_at` -- ou que
        ficou 10 min sem escrever -- e o caso que mais importa, e levava
        minutos demais para aparecer.

        Duas falhas, e nao uma: um unico timeout nao deve acionar o dead-man's
        switch e treinar o operador a ignorar o alarme. Dois ciclos consecutivos
        (~60 s na cadencia real) ja distinguem falha de ruido.
        """
        with self._lock:
            return [s for s, n in self.collect_errors.items() if n >= falhas_seguidas]

    def snapshot(self, max_stale: timedelta) -> dict[str, Any]:
        agora = datetime.now(UTC)
        with self._lock:
            return {
                "status": "ok",
                "uptime_seconds": int((agora - self.started_at).total_seconds()),
                "heartbeat_at": agora.isoformat(),
                "n8n_up": self.n8n_up,
                "last_collect": {k: v.isoformat() for k, v in self.last_collect_at.items()},
                "collect_errors": dict(self.collect_errors),
                "stale_sources": [
                    s for s, t in self.last_collect_at.items() if agora - t > max_stale
                ],
                "fontes_cego": [
                    s for s, n in self.collect_errors.items() if n >= FALHAS_PARA_CEGO
                ],
                "gaps_detected": self.gaps_detected,
                "alertas_sem_destinatario": self.alertas_sem_destinatario,
                "fontes_parciais": dict(self.fontes_parciais),
                "queue": dict(self.queue),
            }


async def _health(request: web.Request) -> web.Response:
    state: HealthState = request.app[HEALTH_KEY]
    max_stale: timedelta = request.app[MAX_STALE_KEY]
    snap = state.snapshot(max_stale)
    # 503 quando a fonte parou de coleta: o cron externo distingue "vivo" de
    # "vivo mas nao coletando", que sao falhas diferentes.
    status = (
        503
        if snap["stale_sources"] or snap["fontes_cego"] or state.n8n_up is False
        else 200
    )
    return web.json_response(snap, status=status)


async def _ready(request: web.Request) -> web.Response:
    return web.json_response({"ready": True})


class HealthServer:
    """Servidor de heartbeat em THREAD e event loop PROPRIOS.

    **Medido, nao suposto.** O agente roda seis jobs no mesmo event loop, e o
    store e psycopg sincrono. Uma chamada de store lenta segura a thread, e
    enquanto a thread esta segura o aiohttp nao aceita conexao nenhuma.

    Medido com o servidor real e um cliente FORA do processo (um cliente dentro
    do loop nao mede nada: ele tambem fica preso):

        controle, sem bloqueio      : 56 ms   HTTP 200
        store de 2,0 s no loop      : 30.026 ms  ReadTimeout

        A requisicao que chegou durante o store nao ficou LENTA: ficou
        INALCANCAVEL. Com o `--max-time 10` do cron do runbook, isso e
        "HERMES-MORTO" -- e o cron existe para pegar exatamente esse caso.
        Cortar o store em `to_thread` resolveria a causa, mas sao 59 pontos de
        chamada em 10 arquivos (medido) e um refactor largo demais para o
        ganho. Separar o heartbeat resolve o modo de falha MEDIDO com mudanca
        em um arquivo e sem tocar no pipeline.

    O loop do agente ainda pode bloquear: as coletagens atrasam, e e para isso
    que `collect_errors` e `fontes_cego` existem -- depois de dois ciclos sem
    coleta, o `/healthz` responde 503 e o operador ve "vivo mas nao coletando".
    Que e exatamente a distincao que o modulo existe para fazer.
    """

    def __init__(self, state: HealthState, *, host: str = "0.0.0.0", port: int = 8099,
                 max_stale: timedelta = timedelta(minutes=10)) -> None:
        self.state = state
        self.host = host
        self.port = port
        self.max_stale = max_stale
        self._runner: web.AppRunner | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pronto = threading.Event()
        self._erro: BaseException | None = None

    async def start(self) -> None:
        self._thread = threading.Thread(
            target=self._rodar, name="agent-nio-health", daemon=True
        )
        self._thread.start()
        # `Event.wait` e de BLOQUEIO: Called direto aqui travaria o loop do
        # agente -- justamente o defeito que esta classe existe para evitar.
        ok = await asyncio.to_thread(self._pronto.wait, 10.0)
        if self._erro is not None:
            raise RuntimeError(f"health server nao subiu: {self._erro}") from self._erro
        if not ok:
            raise TimeoutError("health server nao subiu em 10 s")

    def _rodar(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._subir())
            self._pronto.set()
            loop.run_forever()
        except BaseException as exc:  # noqa: BLE001 - reportar a falha na thread
            self._erro = exc
            self._pronto.set()
        finally:
            with contextlib.suppress(Exception):  # encerrando; nada a fazer
                loop.run_until_complete(self._parar())
            loop.close()

    async def _subir(self) -> None:
        app = web.Application()
        app[HEALTH_KEY] = self.state
        app[MAX_STALE_KEY] = self.max_stale
        app.router.add_get("/healthz", _health)
        app.router.add_get("/healthz/ready", _ready)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("health_server_ouvindo", host=self.host, port=self.port, thread=self._thread.name)

    async def _parar(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def stop(self) -> None:
        # `loop.stop()` nao e corrotina: `run_coroutine_threadsafe` exige uma.
        # E para parar o loop que roda em OUTRA thread, o caminho e
        # `call_soon_threadsafe`.
        if self._loop and self._loop.is_running():
            await asyncio.to_thread(self._loop.call_soon_threadsafe, self._loop.stop)
        if self._thread:
            await asyncio.to_thread(self._thread.join, 10.0)
        self._loop = None
        self._thread = None

    @property
    def porta_real(self) -> int:
        """Porta efetiva. `port=0` pede "qualquer porta livre" ao SO."""
        if self._runner is None:
            raise RuntimeError("servidor nao subiu")
        site = next(iter(self._runner.sites))  # `sites` e um SET
        return site._server.sockets[0].getsockname()[1]  # noqa: SLF001


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