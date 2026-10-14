"""O heartbeat tem de sobreviver a um event loop ocupado.

Medido antes desta correcao, com o servidor real e um cliente FORA do
processo:

    controle, sem bloqueio        : 56 ms        HTTP 200
    store de 2,0 s no loop        : 30.026 ms    ReadTimeout

A requisicao que chegou durante o store nao ficou LENTA: ficou
INALCANCAVEL. Com o `--max-time 10` do cron do runbook, isso e "HERMES-MORTO" --
e o cron existe para pegar exatamente esse caso. Um cliente DENTRO do loop nao
mede isso: ele fica preso junto.

O `HealthServer` passou a rodar em thread e event loop proprios.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta

import httpx
import pytest

from agent_nio_monitor.health import HealthServer, HealthState

ATRASO_SEGUNDOS = 0.6
TIMEOUT_CLIENTE = 5.0
# O /healthz tem de responder em bem menos que o tempo de bloqueio. Metade e
# folga generosa: o alvo do teste nao e "rapido", e "respondeu".
LIMITE_MS = ATRASO_SEGUNDOS * 1000 / 2


def _dispara_fora_do_loop(base: str, evento: threading.Event, out: dict) -> None:
    """Cliente numa thread de verdade.

    Precisa ser thread separada e nao corrotina: corrotina divide o event loop
    com o servidor, entao ficaria presa junto e a medicao nao diria nada --
    foi exatamente o que aconteceu nas duas primeiras versoes da sonda.
    """
    evento.wait(timeout=TIMEOUT_CLIENTE)
    t = time.perf_counter()
    try:
        with httpx.Client(timeout=TIMEOUT_CLIENTE) as c:
            r = c.get(f"{base}/healthz")
        out["status"] = r.status_code
    except Exception as exc:  # noqa: BLE001
        out["status"] = f"{type(exc).__name__}"
    out["ms"] = (time.perf_counter() - t) * 1000


async def _ciclo(base: str, bloquear: bool) -> dict:
    evento = threading.Event()
    out: dict = {}
    cliente = threading.Thread(target=_dispara_fora_do_loop, args=(base, evento, out))
    cliente.start()

    await asyncio.sleep(0.15)
    evento.set()

    # Sem NENHUM `await` entre `set()` e o bloqueio: um `await` daria ao loop a
    # chance de atender a requisicao ANTES de travar, e o teste passaria sem
    # provar nada.
    if bloquear:
        time.sleep(ATRASO_SEGUNDOS)  # o que o psycopg sincrono faz
        await asyncio.sleep(0)
    else:
        await asyncio.sleep(ATRASO_SEGUNDOS)

    cliente.join(timeout=TIMEOUT_CLIENTE + 5)
    return out


@pytest.fixture
async def servidor() -> tuple[HealthServer, HealthState]:
    estado = HealthState()
    estado.mark("n8n_executions", True)
    srv = HealthServer(estado, host="127.0.0.1", port=0)
    await srv.start()
    try:
        yield f"http://127.0.0.1:{srv.porta_real}", estado
    finally:
        await srv.stop()


class TestHeartbeatSobreviveAoLoopOcupado:
    async def test_healthz_responde_mesmo_com_o_loop_ocupado(self, servidor) -> None:
        """O teste que fixa a correcao.

        Limite generoso de propósito: nao quer provar "mais rapido", quer
        provar "respondeu enquanto o loop estava ocupado". Com o store no loop
        unico, a resposta anterior era `ReadTimeout` -- nao um numero alto, uma
        ausencia de resposta.
        """
        base, _estado = servidor

        controle = await _ciclo(base, bloquear=False)
        assert controle["status"] == 200, controle

        durante = await _ciclo(base, bloquear=True)

        assert durante["status"] == 200, (
            f"/healthz ficou inacessivel com o loop ocupado: {durante}. "
            "O HealthServer tem de rodar em thread propria."
        )
        assert durante["ms"] < LIMITE_MS, (
                    f"/healthz demorou {durante['ms']:.0f} ms para um loop ocupado por "
                    f"{ATRASO_SEGUNDOS:.1f} s -- esta atendendo ou apenas nao expirou?"
                )


class TestEstadoCompartilhado:
    """`HealthState` e escrito pelo agente e lido de outra thread."""

    def test_snapshot_consistente_sob_concorrencia(self) -> None:
        """Sem o lock, `snapshot()` pode iterar um dict enquanto o outro lado
        insere -- "dictionary changed size during iteration", e a resposta 500
        que o dead-man's switch leria como queda."""
        import threading as th

        estado = HealthState()
        erros: list[BaseException] = []
        parar = th.Event()

        def _escreve() -> None:
            i = 0
            while not parar.is_set():
                i += 1
                try:
                    estado.mark(f"fonte-{i % 7}", i % 2 == 0)
                    estado.registra_fonte_parcial(f"fonte-{i % 7}", {"falhas": i % 5})
                except Exception as exc:  # noqa: BLE001
                    erros.append(exc)
                    return

        def _le() -> None:
            for _ in range(400):
                try:
                    estado.snapshot(timedelta(minutes=10))
                except Exception as exc:  # noqa: BLE001
                    erros.append(exc)
                    return

        escritores = [th.Thread(target=_escreve) for _ in range(3)]
        leitores = [th.Thread(target=_le) for _ in range(3)]
        for t in [*escritores, *leitores]:
            t.start()
        for t in leitores:
            t.join(timeout=10)
        parar.set()
        for t in escritores:
            t.join(timeout=10)

        assert not erros, f"acesso concorrente quebrou: {erros[0]!r}"

    def test_contadores_passam_por_metodo_e_nao_por_atributo(self) -> None:
        """`+=` em atributo e leitura-e-escrita: um `snapshot()` concorrente
        pode ler no meio dela. Por isso os contadores sao metodos."""
        import inspect

        fonte = inspect.getsource(HealthState)
        assert "def conta_gap" in fonte
        assert "def conta_alerta_perdido" in fonte
        assert "self.gaps_detected += 1" in fonte, "o incremento precisa estar dentro do lock"
