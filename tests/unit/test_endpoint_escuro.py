"""Endpoint escuro: o monitor nao pode dizer que esta saudavel enquanto nao ve.

`ContainerCollector` e `ResourceCollector` faziam `continue` numa falha de
`list_containers`. `CollectorResult.error` ficava `None`, e `Agent._ciclo`
chamava `health.mark(chave, resultado.ok)` com `ok=True`. Um endpoint inteiro
do Portainer podia ficar fora do ar indefinidamente e o `/healthz` continuava
verde -- que e o pior defeito possivel num monitor cujo modo de falha declarado
e silencio.

O que se trava aqui:
1. parcial (`failures > 0`) distingue de total (`error`), e ambos sao visiveis;
2. escuridao NAO gera `container_removed` falso -- o ciclo de vida so avanca
   em leitura bem-sucedida;
3. a fonte cega derruba o `/healthz` depois de 2 ciclos consecutivos.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx

from agent_nio_monitor.health import HealthServer, HealthState
from agent_nio_monitor.sources.portainer.client import PortainerClient
from agent_nio_monitor.sources.portainer.collector import ContainerCollector
from agent_nio_monitor.sources.portainer.stats import ResourceCollector
from agent_nio_monitor.store.memory_repository import InMemoryRepository

PTAI = "https://portainer.test"


def _base_do_config() -> str:
    """URL real do Portainer em `config/environments.yaml`.

    O `Agent` monta o cliente a partir da config, entao um teste de integracao
    tem que interceptar essa URL -- e nao um host inventado.
    """
    from agent_nio_monitor.config.loader import load

    return load().active.portainer.url.rstrip("/")


def _dois_endpoints(base: str = PTAI) -> None:
    respx.get(f"{base}/api/endpoints").mock(
        return_value=httpx.Response(200, json=[{"Id": 1}, {"Id": 2}])
    )
    respx.get(f"{base}/api/stacks").mock(return_value=httpx.Response(200, json=[]))


def _um_endpoint(base: str = PTAI) -> None:
    respx.get(f"{base}/api/endpoints").mock(
        return_value=httpx.Response(200, json=[{"Id": 1}])
    )
    respx.get(f"{base}/api/stacks").mock(return_value=httpx.Response(200, json=[]))


def _containers(eid: int, rows: list[dict], status: int = 200, base: str = PTAI) -> None:
    respx.get(f"{base}/api/endpoints/{eid}/docker/containers/json").mock(
        return_value=httpx.Response(status, json=rows)
    )


def _logs(eid: int = 1, base: str = PTAI) -> None:
    respx.get(url__regex=rf"{base}/api/endpoints/{eid}/docker/containers/.+/logs").mock(
        return_value=httpx.Response(200, content=b"")
    )


def _stats(eid: int = 1, base: str = PTAI) -> None:
    respx.get(url__regex=rf"{base}/api/endpoints/{eid}/docker/containers/.+/stats").mock(
        return_value=httpx.Response(200, json={"cpu_stats": {}, "memory_stats": {}})
    )


class TestEscuridaoParcial:
    @respx.mock
    async def test_um_de_dois_escuros_e_visivel_sem_matar_a_fonte(self) -> None:
        """Parcial nao e total: o outro endpoint ainda responde, entao a fonte
        NAO pode virar `error` -- mas o endpoint que caiu tem que aparecer."""
        _dois_endpoints()
        _containers(1, [{"Id": "a", "Names": ["/w1"], "State": "running", "Status": "Up"}])
        _containers(2, [], status=503)
        _logs()

        col = ContainerCollector(PortainerClient(PTAI, "tok"), InMemoryRepository())
        r = await col.check_state()

        assert r.ok is True, "parcial nao pode derrubar a fonte inteira"
        assert r.failures == 1
        assert r.extra["endpoints_escuros"] == [2]
        assert r.collected == 1, "o endpoint que responde ainda foi lido"

    @respx.mock
    async def test_escuridao_parcial_no_consumo_tambem(self) -> None:
        _dois_endpoints()
        _containers(1, [])
        _containers(2, [], status=503)
        _stats()

        rc = ResourceCollector(PortainerClient(PTAI, "tok"), InMemoryRepository())
        r = await rc.check_resources()

        assert r.ok is True
        assert r.failures == 1
        assert r.extra["endpoints_escuros"] == [2]


class TestEscuridaoTotal:
    @respx.mock
    async def test_nenhum_endpoint_responde_vira_erro(self) -> None:
        """A distincao que antes nao existia. Sem ela, "ok" significava "vi pelo
        menos um", e um Portainer com todos os endpoints fora do ar -- ou com
        todos apagados -- continuava parecendo saudavel."""
        _dois_endpoints()
        _containers(1, [], status=503)
        _containers(2, [], status=503)

        col = ContainerCollector(PortainerClient(PTAI, "tok"), InMemoryRepository())
        r = await col.check_state()

        assert r.ok is False, "escuridao total tem que virar `error`"
        assert r.error is not None
        assert r.failures == 2

    @respx.mock
    async def test_escuridao_total_no_consumo_vira_erro(self) -> None:
        _dois_endpoints()
        _containers(1, [], status=503)
        _containers(2, [], status=503)

        rc = ResourceCollector(PortainerClient(PTAI, "tok"), InMemoryRepository())
        r = await rc.check_resources()

        assert r.ok is False
        assert r.failures == 2

    @respx.mock
    async def test_falha_na_listagem_de_endpoints_continua_sendo_erro(self) -> None:
        """O caminho que ja existia e nao pode regredir."""
        respx.get(f"{PTAI}/api/endpoints").mock(return_value=httpx.Response(503))
        col = ContainerCollector(PortainerClient(PTAI, "tok"), InMemoryRepository())
        assert (await col.check_state()).ok is False


class TestEscuridaoNaoFalsificaRemocao:
    """O risco real de reportar endpoint escuro como "vazio".

    Se `LifecycleTracker.removidos()` fosse chamado com lista vazia durante a
    escuridao, `CONFIRMACOES_DE_AUSENCIA=2` dispararia `container_removed` para
    TODO container do endpoint -- uma tempestade de falso positivo gerada
    exatamente pela falha que o monitor existe para reportar.

    O codigo nao chama `_processar_endpoint` quando a listagem falha, entao o
    contador de ausencia nao avanca. Este teste trava essa garantia.
    """

    @respx.mock
    async def test_endpoint_escuro_nao_gera_container_removed(self) -> None:
        col = ContainerCollector(PortainerClient(PTAI, "tok"), InMemoryRepository())

        _um_endpoint()
        _containers(1, [{"Id": "a", "Names": ["/w1"], "State": "running", "Status": "Up"}])
        _logs()
        await col.check_state()

        # tres ciclos escuros: bem mais que as 2 confirmacoes exigidas
        for _ in range(3):
            _containers(1, [], status=503)
            r = await col.check_state()
            assert r.ok is False

        # o endpoint volta com o MESMO container: nada foi removido
        _containers(1, [{"Id": "a", "Names": ["/w1"], "State": "running", "Status": "Up"}])
        r = await col.check_state()
        assert r.ok is True
        assert [e.event_type for e in r.extra["eventos"]] == [], (
            "endpoint que volta com o mesmo container nao pode virar "
            "container_removed -- seria alarme falso"
        )

    @respx.mock
    async def test_remocao_real_ainda_e_detectada(self) -> None:
        """O oposto: o guard acima nao pode neutralizar a deteccao de verdade."""
        col = ContainerCollector(PortainerClient(PTAI, "tok"), InMemoryRepository())
        _um_endpoint()
        _containers(1, [{"Id": "a", "Names": ["/w1"], "State": "running", "Status": "Up"}])
        _logs()
        await col.check_state()

        for _ in range(2):
            _containers(1, [])
            r = await col.check_state()
        tipos = [e.event_type for e in r.extra["eventos"]]
        assert tipos == ["container_removed"], f"remocao real nao detectada: {tipos}"


class TestFonteCegaDerrubaHealth:
    def test_duas_falhas_consecutivas_marcam_a_fonte_cega(self) -> None:
        """Uma falha nao basta: um timeout unico acionaria o dead-man's switch e
        treinaria o operador a ignorar. Duas, a ~30 s de cadencia, ja distinguem
        falha de ruido."""
        estado = HealthState()
        estado.mark("portainer_containers", False)
        assert estado.fontes_cego() == []

        estado.mark("portainer_containers", False)
        assert estado.fontes_cego() == ["portainer_containers"]

    def test_recuperacao_zera_a_contagem(self) -> None:
        estado = HealthState()
        estado.mark("portainer_containers", False)
        estado.mark("portainer_containers", False)
        assert estado.fontes_cego() == ["portainer_containers"]

        estado.mark("portainer_containers", True)
        assert estado.fontes_cego() == []
        assert estado.collect_errors["portainer_containers"] == 0

    async def test_healthz_503_quando_a_fonte_esta_cega(self) -> None:
        """O `/healthz` precisa cair a-folha. E o que o cron externo do runbook
        consome com `curl -f`: sem 503, a falha do Portainer nao chega a ninguem."""
        estado = HealthState()
        estado.mark("n8n_executions", True)
        servidor = HealthServer(estado, host="127.0.0.1", port=0, max_stale=timedelta(minutes=10))
        await servidor.start()
        try:
            site = next(iter(servidor._runner.sites))  # noqa: SLF001
            base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"  # noqa: SLF001

            async with httpx.AsyncClient() as c:
                assert (await c.get(f"{base}/healthz")).status_code == 200

                estado.mark("portainer_containers", False)
                estado.mark("portainer_containers", False)
                r = await c.get(f"{base}/healthz")

            assert r.status_code == 503, "fonte cega precisa derrubar o /healthz"
            assert r.json()["fontes_cego"] == ["portainer_containers"]
        finally:
            await servidor.stop()


class TestIntegracaoComHealth:
    """Fecha o ciclo: o que o coletor produz alimenta o que o health responde."""

    async def test_escuridao_total_chega_ao_health_como_falha(self, respx_mock) -> None:
        from agent_nio_monitor.config.loader import load
        from agent_nio_monitor.main import Agent

        base = _base_do_config()
        _dois_endpoints(base)
        _containers(1, [], status=503, base=base)
        _containers(2, [], status=503, base=base)

        agent = Agent(load(), InMemoryRepository())
        assert agent.containers is not None, "sem coletor de container nao ha o que testar"

        await agent.check_containers()
        snap = agent.health.snapshot(timedelta(minutes=10))

        assert snap["collect_errors"]["portainer_containers"] == 1
        assert snap["fontes_cego"] == [], "um ciclo ainda nao cega -- por desenho"
        assert snap["fontes_parciais"]["portainer_containers"]["falhas"] == 2

        await agent.check_containers()
        snap2 = agent.health.snapshot(timedelta(minutes=10))
        assert snap2["fontes_cego"] == ["portainer_containers"]

    async def test_falha_parcial_some_do_health_quando_se_recupera(self, respx_mock) -> None:
        from agent_nio_monitor.config.loader import load
        from agent_nio_monitor.main import Agent

        base = _base_do_config()
        _dois_endpoints(base)
        _containers(1, [], base=base)
        _containers(2, [], status=503, base=base)
        _logs(base=base)

        agent = Agent(load(), InMemoryRepository())
        await agent.check_containers()
        assert agent.health.fontes_parciais, "a falha parcial tem que aparecer"

        _containers(2, [], base=base)
        await agent.check_containers()
        assert agent.health.fontes_parciais == {}, (
            "endpoint recuperado tem que limpar o registro de falha parcial -- "
            "deixar para sempre seria um monitor que nunca volta ao normal"
        )


@pytest.mark.parametrize("falhas_para_cego", [1, 2, 3])
def test_limite_de_falhas_consecutivas_e_configuravel(falhas_para_cego: int) -> None:
    estado = HealthState()
    for _ in range(falhas_para_cego - 1):
        estado.mark("x", False)
    assert estado.fontes_cego(falhas_para_cego) == []
    estado.mark("x", False)
    assert estado.fontes_cego(falhas_para_cego) == ["x"]