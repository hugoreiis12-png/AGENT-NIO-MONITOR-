"""Reconferencia: o problema que gerou o alerta ainda existe?"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agent_nio_monitor.config.schema import PollingConfig, ThresholdsConfig
from agent_nio_monitor.notify.resolution import ResolutionChecker, espera_em_segundos
from agent_nio_monitor.sources.n8n.client import N8NApiError
from agent_nio_monitor.store.repository import EventRecord
from agent_nio_monitor.store.memory_repository import InMemoryRepository

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
LIMITES = ThresholdsConfig(cpu_host_pct=80, mem_pct=90)


class Containers:
    def __init__(self, atuais: dict | None = None, nomes: dict | None = None) -> None:
        self.atuais, self.nomes = atuais or {}, nomes or {}

    def estado_atual(self, container_id: str) -> dict | None:
        return self.atuais.get(container_id)

    def nome_presente(self, endpoint_id: int, nome: str) -> bool | None:
        nomes = self.nomes.get(endpoint_id)
        return None if nomes is None else nome in nomes


class Recursos:
    def __init__(self, amostras: dict | None = None) -> None:
        self.ultima_amostra = amostras or {}


class N8N:
    def __init__(self, ultima: dict | None = None, erro: bool = False) -> None:
        self.ultima, self.erro = ultima, erro

    async def last_execution(self, workflow_id: str) -> dict | None:
        if self.erro:
            raise N8NApiError("fora do ar")
        return self.ultima


def _ev(tipo: str, resource_id: str = "abc", **detail) -> EventRecord:
    return EventRecord(id=uuid4(), ts=T0, source="x", event_type=tipo, severity="high",
                       title="t", resource_id=resource_id, detail=detail)


def _checador(containers=None, resources=None, n8n=None, repo=None) -> ResolutionChecker:  # noqa: ANN001
    return ResolutionChecker(containers=containers, resources=resources, n8n=n8n or N8N(),
                             repo=repo or InMemoryRepository(), limites=LIMITES)


def _c(state="running", health=None, recente=False) -> dict:
    return {"state": state, "health": health, "recente": recente}


class TestContainer:
    async def test_parado_que_voltou_a_rodar_esta_resolvido(self) -> None:
        c = _checador(Containers({"abc": _c("running")}))
        assert await c.motivo(_ev("container_stopped")) == "container voltou a rodar"

    async def test_parado_que_continua_parado_nao_esta_resolvido(self) -> None:
        assert await _checador(Containers({"abc": _c("exited")})).motivo(_ev("container_stopped")) is None

    async def test_container_que_nao_apareceu_no_ciclo_nao_conta_como_resolvido(self) -> None:
        assert await _checador(Containers({})).motivo(_ev("container_stopped")) is None

    async def test_unhealthy_so_resolve_quando_o_healthcheck_normaliza(self) -> None:
        ev = _ev("container_unhealthy")
        assert await _checador(Containers({"abc": _c("running", "unhealthy")})).motivo(ev) is None
        assert await _checador(Containers({"abc": _c("running", "healthy")})).motivo(ev) is not None
        assert await _checador(Containers({"abc": _c("exited", "healthy")})).motivo(ev) is None

    async def test_removido_que_voltou_a_existir_esta_resolvido(self) -> None:
        ev = _ev("container_removed", endpoint_id=3, recurso="worker")
        assert await _checador(Containers(nomes={3: {"worker", "api"}})).motivo(ev) == "container voltou a existir"
        assert await _checador(Containers(nomes={3: {"api"}})).motivo(ev) is None
        assert await _checador(Containers(nomes={})).motivo(ev) is None

    async def test_loop_resolve_quando_estabiliza(self) -> None:
        ev = _ev("container_restart_loop")
        assert await _checador(Containers({"abc": _c("running", recente=False)})).motivo(ev) == "container estabilizou"
        assert await _checador(Containers({"abc": _c("running", recente=True)})).motivo(ev) is None
        assert await _checador(Containers({"abc": _c("restarting", recente=True)})).motivo(ev) is None

    async def test_sem_portainer_nada_e_resolvido(self) -> None:
        assert await _checador(None).motivo(_ev("container_stopped")) is None


class TestConsumo:
    async def test_resolve_quando_ficou_abaixo_dos_dois_limites(self) -> None:
        ev = _ev("container_resource_high")
        assert await _checador(resources=Recursos({"abc": (10.0, 40.0)})).motivo(ev) == "consumo voltou ao normal"

    async def test_continua_se_qualquer_um_segue_alto(self) -> None:
        ev = _ev("container_resource_high")
        assert await _checador(resources=Recursos({"abc": (10.0, 95.0)})).motivo(ev) is None
        assert await _checador(resources=Recursos({"abc": (85.0, 10.0)})).motivo(ev) is None

    async def test_sem_amostra_nao_resolve(self) -> None:
        ev = _ev("container_resource_high")
        assert await _checador(resources=Recursos({})).motivo(ev) is None
        assert await _checador(resources=Recursos({"abc": (None, None)})).motivo(ev) is None


class TestExecucao:
    def _ev(self) -> EventRecord:
        return _ev("execution_failed", "wf", workflow_id="wf", ultima_falha_ts="2026-10-13T11:50:00+00:00")

    async def test_sucesso_posterior_a_falha_resolve(self) -> None:
        ultima = {"status": "success", "startedAt": "2026-10-13T11:55:00.000Z"}
        assert await _checador(n8n=N8N(ultima)).motivo(self._ev()) == "execucao posterior com sucesso"

    async def test_sucesso_antigo_nao_resolve(self) -> None:
        ultima = {"status": "success", "startedAt": "2026-10-13T11:00:00.000Z"}
        assert await _checador(n8n=N8N(ultima)).motivo(self._ev()) is None

    async def test_nova_falha_nao_resolve(self) -> None:
        ultima = {"status": "error", "startedAt": "2026-10-13T11:59:00.000Z"}
        assert await _checador(n8n=N8N(ultima)).motivo(self._ev()) is None

    async def test_sem_execucao_ou_n8n_fora_do_ar_nao_resolve(self) -> None:
        assert await _checador(n8n=N8N(None)).motivo(self._ev()) is None
        assert await _checador(n8n=N8N(erro=True)).motivo(self._ev()) is None


class TestToggleDeWorkflow:
    @pytest.mark.parametrize(("tipo", "ativo", "snapshot", "resolvido"), [
        ("workflow_activated", True, False, True),
        ("workflow_activated", True, True, False),
        ("workflow_deactivated", False, True, True),
        ("workflow_deactivated", False, False, False),
    ])
    async def test_resolve_quando_o_estado_voltou_ao_anterior(self, tipo, ativo, snapshot, resolvido) -> None:  # noqa: ANN001
        repo = InMemoryRepository()
        repo.save_workflow_snapshot("wf", "v1", snapshot, {"id": "wf"})
        motivo = await _checador(repo=repo).motivo(_ev(tipo, "wf", workflow_id="wf", ativo=ativo))
        assert (motivo is not None) is resolvido


class TestFatosNuncaResolvem:
    @pytest.mark.parametrize("tipo", ["workflow_changed", "workflow_created", "stack_changed", "credential_updated"])
    async def test_sempre_segue_para_o_envio(self, tipo: str) -> None:
        assert await _checador().motivo(_ev(tipo)) is None


class TestEspera:
    def test_estado_espera_um_ciclo_da_fonte_mais_a_margem(self) -> None:
        p = PollingConfig()
        assert espera_em_segundos("execution_failed", p) == 35
        assert espera_em_segundos("container_stopped", p) == 35

    def test_fonte_lenta_e_cortada_no_teto(self) -> None:
        p = PollingConfig()
        assert espera_em_segundos("container_resource_high", p) == 40
        assert espera_em_segundos("workflow_activated", p) == 40

    @pytest.mark.parametrize("tipo", [
        "execution_failed", "container_stopped", "container_unhealthy", "container_removed",
        "container_restart_loop", "container_resource_high", "workflow_activated", "workflow_deactivated",
    ])
    def test_espera_mais_um_tick_nunca_passa_de_50s(self, tipo: str) -> None:
        p = PollingConfig()
        assert espera_em_segundos(tipo, p) + p.queue_seconds <= 50

    def test_fatos_saem_sem_espera(self) -> None:
        p = PollingConfig()
        for tipo in ("workflow_changed", "workflow_created", "stack_changed"):
            assert espera_em_segundos(tipo, p) == 0
