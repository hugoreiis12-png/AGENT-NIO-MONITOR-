"""Resiliência de rede e do servidor de health.

Duas coisas que nao podem passar sem teste:

1. **5xx tem que repetir.** Durante restart do n8n o gateway responde 502/503, e
   e exatamente entao que insistir importa. A versao anterior so repetia em
   timeout/rede, entao um pico de 503 registrava erro e o agente passava 60 s as
   cegas -- cego justamente no incidente.

2. **O HealthServer e o dead-man's switch.** Se ele cair, o cron externo perde o
   alvo e o monitor falha em silencio. Estava a 49% de cobertura.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx

from agent_nio_monitor.health import (
    HealthServer,
    HealthState,
    deadman_curl_example,
    log_linha_alerta,
)
from agent_nio_monitor.sources.n8n.client import (
    CursorInvalid,
    N8NApiError,
    N8NClient,
    _deve_retryar,
)
from agent_nio_monitor.sources.portainer.client import PortainerApiError
from agent_nio_monitor.sources.portainer.client import _deve_retryar as _pt_retryar

N8N = "http://n8n.test"
PTAI = "https://portainer.test"


class TestPoliticaDeRetry:
    @pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
    def test_5xx_e_rate_limit_sao_transitorios(self, status: int) -> None:
        assert _deve_retryar(N8NApiError("x", status)) is True
        assert _pt_retryar(PortainerApiError("x", status)) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_4xx_nao_repite(self, status: int) -> None:
        """Cursor invalidado e parametro invalido nao melhoram com tentativa."""
        assert _deve_retryar(N8NApiError("x", status)) is False
        assert _pt_retryar(PortainerApiError("x", status)) is False

    def test_cursor_invalido_nunca_repite(self) -> None:
        exc = CursorInvalid("cursor morto", 404)
        assert _deve_retryar(exc) is False

    def test_erro_de_rede_repite(self) -> None:
        assert _deve_retryar(httpx.ConnectError("recusado")) is True
        assert _deve_retryar(httpx.ReadTimeout("estourou")) is True

    def test_excecao_desconhecida_nao_repite(self) -> None:
        assert _deve_retryar(ValueError("bug de programacao")) is False


class TestRetryNaPratica:
    @respx.mock
    async def test_503_entao_200_produz_um_resultado(self) -> None:
        rota = respx.get(f"{N8N}/api/v1/executions").mock(
            side_effect=[
                httpx.Response(503, json={}),
                httpx.Response(503, json={}),
                httpx.Response(200, json={"data": []}),
            ]
        )
        async with N8NClient(N8N, "k") as c:
            dados = await c._get("/api/v1/executions")
        assert dados == {"data": []}
        assert rota.call_count == 3, "deveria ter tentado 3x antes de desistir"

    @respx.mock
    async def test_503_persistente_levanta_apos_o_limite(self) -> None:
        respx.get(f"{N8N}/api/v1/executions").mock(
            return_value=httpx.Response(503, json={})
        )
        async with N8NClient(N8N, "k") as c:
            with pytest.raises(N8NApiError) as ei:
                await c._get("/api/v1/executions")
        assert ei.value.status == 503

    @respx.mock
    async def test_403_nao_gasta_tentativas(self) -> None:
        """403 e permissao: insistir so gasta tempo e pode gerar lockout."""
        rota = respx.get(f"{N8N}/api/v1/users").mock(
            return_value=httpx.Response(403, json={})
        )
        async with N8NClient(N8N, "k") as c:
            with pytest.raises(N8NApiError, match="owner/admin"):
                await c.list_users()
        assert rota.call_count == 1

    @respx.mock
    async def test_portainer_503_repite(self) -> None:
        rota = respx.get(f"{PTAI}/api/stacks").mock(
            side_effect=[
                httpx.Response(503, json={}),
                httpx.Response(200, json=[]),
            ]
        )
        from agent_nio_monitor.sources.portainer.client import PortainerClient

        async with PortainerClient(PTAI, "k") as c:
            assert await c.list_stacks() == []
        assert rota.call_count == 2


class TestHealthState:
    def test_snapshot_marca_fonte_stale(self) -> None:
        h = HealthState()
        h.mark("n8n", True)
        assert h.snapshot(timedelta(minutes=10))["stale_sources"] == []

    def test_n8n_fora_marca_down(self) -> None:
        h = HealthState()
        h.n8n_up = False
        snap = h.snapshot(timedelta(minutes=10))
        assert snap["n8n_up"] is False

    def test_contador_de_buraco_cresce(self) -> None:
        h = HealthState()
        h.gaps_detected += 3
        assert h.snapshot(timedelta(minutes=10))["gaps_detected"] == 3

    def test_uptime_nao_e_negativo(self) -> None:
        assert HealthState().snapshot(timedelta(minutes=1))["uptime_seconds"] >= 0


class TestServidorDeHealth:
    """Sobe o servidor aiohttp de verdade e fala HTTP com ele."""

    @pytest.fixture
    async def servidor(self):
        state = HealthState()
        state.mark("n8n_executions", True)
        srv = HealthServer(state, host="127.0.0.1", port=0, max_stale=timedelta(minutes=10))
        await srv.start()
        # `AppRunner.sites` e um SET, nao lista: precisa iterar para achar a porta
        # que o OS escolheu (port=0 pede "qualquer porta livre").
        site = next(iter(srv._runner.sites))  # noqa: SLF001
        porta = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        yield f"http://127.0.0.1:{porta}", state
        await srv.stop()

    async def test_healthz_200_quando_tudo_verde(self, servidor) -> None:
        base, _ = servidor
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{base}/healthz")
        assert r.status_code == 200
        corpo = r.json()
        assert corpo["status"] == "ok"
        assert "n8n_executions" in corpo["last_collect"]
        assert "heartbeat_at" in corpo

    async def test_healthz_503_quando_n8n_esta_fora(self, servidor) -> None:
        base, state = servidor
        state.n8n_up = False
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{base}/healthz")
        assert r.status_code == 503, (
            "o cron externo precisa distinguir 'vivo' de 'vivo mas sem n8n'"
        )
        assert r.json()["n8n_up"] is False

    async def test_healthz_503_quando_fonte_stale(self, servidor) -> None:
        base, state = servidor
        from datetime import UTC, datetime

        state.last_collect_at["n8n_executions"] = datetime.now(UTC) - timedelta(minutes=30)
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{base}/healthz")
        assert r.status_code == 503
        assert "n8n_executions" in r.json()["stale_sources"]

    async def test_ready_responde(self, servidor) -> None:
        base, _ = servidor
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{base}/healthz/ready")
        assert r.status_code == 200
        assert r.json() == {"ready": True}

    async def test_rota_desconhecida_404(self, servidor) -> None:
        base, _ = servidor
        async with httpx.AsyncClient() as c:
            assert (await c.get(f"{base}/dashboard")).status_code == 404

    async def test_server_para_limpamente(self) -> None:
        srv = HealthServer(HealthState(), host="127.0.0.1", port=0)
        await srv.start()
        await srv.stop()
        await srv.stop()  # idempotente


class TestRunbookDoDeadman:
    def test_comando_tem_exit_code_de_falha(self) -> None:
        """O comando precisa FALHAR quando nao ha heartbeat -- e isso que o cron
        externo transforma em alerta. Se o curl sair com 0, o monitor fica mudo."""
        cmd = deadman_curl_example("http://hermes:8099/healthz")
        assert "-fsS" in cmd, "sem -f o curl sai com 0 mesmo em erro HTTP"
        assert "|| echo" in cmd
        assert "HERMES-MORTO" in cmd

    def test_linha_de_alerta_tem_prefixo(self) -> None:
        import json

        linha = log_linha_alerta({"subject": "x", "para": ["a@b.c"]})
        dados = json.loads(linha)
        assert dados["prefix"] == "HERMES"
        assert dados["kind"] == "alerta"