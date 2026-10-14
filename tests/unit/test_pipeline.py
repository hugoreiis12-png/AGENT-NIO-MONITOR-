"""Pipeline: grava, enfileira com a espera certa e despacha so o que ainda vale."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from agent_nio_monitor.config.loader import load, load_policy
from agent_nio_monitor.health import HealthState
from agent_nio_monitor.notify.mailer import DispatchResult
from agent_nio_monitor.notify.pipeline import EventPipeline
from agent_nio_monitor.rules.engine import RuleEngine
from agent_nio_monitor.store.queue import InMemoryQueue, agora
from agent_nio_monitor.store.repository import EventRecord
from agent_nio_monitor.store.memory_repository import InMemoryRepository


class Checador:
    def __init__(self, motivo: str | None = None) -> None:
        self.motivo_fixo = motivo

    async def motivo(self, evento: EventRecord) -> str | None:
        return self.motivo_fixo


class Envio:
    def __init__(self) -> None:
        self.enviados: list[EventRecord] = []

    async def __call__(self, evento: EventRecord) -> DispatchResult:
        self.enviados.append(evento)
        return DispatchResult(status="sent", channel="n8n_webhook")


@pytest.fixture
def cfg(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    monkeypatch.setenv("N8N_API_KEY", "k")
    return load()


def _pipeline(cfg, checador=None):  # noqa: ANN001, ANN202
    repo, fila, envio, saude = InMemoryRepository(), InMemoryQueue(), Envio(), HealthState()
    p = EventPipeline(repo=repo, queue=fila, rules=RuleEngine(load_policy()), polling=cfg.polling,
                      health=saude, checker=checador or Checador(), dispatch=envio)
    return p, repo, fila, envio, saude


def _ev(tipo: str, recurso: str = "r", **detail) -> EventRecord:
    return EventRecord(id=uuid4(), ts=agora(), source="x", event_type=tipo, severity="high",
                       title=f"{tipo}", resource_id=recurso, detail={"notificar": True, **detail})


class TestEnfileirar:
    def test_grava_o_evento_e_enfileira(self, cfg) -> None:  # noqa: ANN001
        p, repo, fila, _, _ = _pipeline(cfg)
        assert p.enqueue([_ev("container_stopped")]) == 1
        assert len(repo.events) == 1 and len(fila.items) == 1

    def test_evento_sem_notificar_so_e_gravado(self, cfg) -> None:  # noqa: ANN001
        p, repo, fila, _, _ = _pipeline(cfg)
        assert p.enqueue([_ev("container_stopped", notificar=False)]) == 0
        assert len(repo.events) == 1 and fila.items == []

    def test_estado_espera_um_ciclo_e_fato_sai_na_hora(self, cfg) -> None:  # noqa: ANN001
        p, _, fila, _, _ = _pipeline(cfg)
        p.enqueue([_ev("container_stopped", "a"), _ev("workflow_changed", "b")])
        por_tipo = {i.event_type: (i.ready_at - i.created_at).total_seconds() for i in fila.items}
        assert por_tipo["container_stopped"] == 35
        assert por_tipo["workflow_changed"] == 0

    def test_mesmo_problema_duas_vezes_vira_um_pendente(self, cfg) -> None:  # noqa: ANN001
        p, _, fila, _, _ = _pipeline(cfg)
        assert p.enqueue([_ev("container_stopped", "a"), _ev("container_stopped", "a")]) == 1
        assert len(fila.items) == 1

    def test_duas_alteracoes_do_mesmo_workflow_sao_dois_avisos(self, cfg) -> None:  # noqa: ANN001
        p, _, fila, _, _ = _pipeline(cfg)
        assert p.enqueue([_ev("workflow_changed", "wf"), _ev("workflow_changed", "wf")]) == 2
        assert len(fila.items) == 2


class TestDespachar:
    async def test_fato_sai_no_primeiro_ciclo(self, cfg) -> None:  # noqa: ANN001
        p, _, _, envio, _ = _pipeline(cfg)
        p.enqueue([_ev("workflow_changed", "wf")])
        resultado = await p.flush()
        assert resultado["enviados"] == 1 and len(envio.enviados) == 1

    async def test_problema_resolvido_antes_do_prazo_nunca_e_enviado(self, cfg) -> None:  # noqa: ANN001
        p, _, fila, envio, _ = _pipeline(cfg, Checador("container voltou a rodar"))
        p.enqueue([_ev("container_stopped", "a")])
        fila.items[0].ready_at = agora() - timedelta(seconds=1)
        resultado = await p.flush()
        assert resultado["descartados"] == 1 and envio.enviados == []

    async def test_estado_ainda_nao_pronto_fica_na_fila(self, cfg) -> None:  # noqa: ANN001
        p, _, fila, envio, _ = _pipeline(cfg)
        p.enqueue([_ev("container_stopped", "a")])
        await p.flush()
        assert envio.enviados == [] and fila.items[0].status == "pending"

    async def test_flush_atualiza_o_estado_de_saude(self, cfg) -> None:  # noqa: ANN001
        p, _, _, _, saude = _pipeline(cfg)
        p.enqueue([_ev("container_stopped", "a")])
        await p.flush()
        assert saude.queue["pending"] == 1
        assert "queue" in saude.snapshot(timedelta(minutes=5))

    async def test_re_enfileirar_depois_de_enviado_nao_duplica_dentro_da_janela(self, cfg) -> None:  # noqa: ANN001
        p, _, fila, envio, _ = _pipeline(cfg)
        p.enqueue([_ev("container_stopped", "a")])
        fila.items[0].ready_at = agora() - timedelta(seconds=1)
        await p.flush()
        assert p.enqueue([_ev("container_stopped", "a")]) == 0
        assert len(envio.enviados) == 1


class TestTempoNaFila:
    @pytest.mark.parametrize("tipo", ["execution_failed", "container_stopped", "container_resource_high"])
    @pytest.mark.parametrize("fase", [0.001, 0.5, 0.999])
    async def test_alerta_persistente_sai_em_ate_50s(self, cfg, tipo: str, fase: float) -> None:  # noqa: ANN001
        """Ticks reais a cada `queue_seconds`, em qualquer fase em relacao a entrada do alerta."""
        p, _, fila, envio, _ = _pipeline(cfg)
        p.enqueue([_ev(tipo, "a")])
        entrada, passo = fila.items[0].created_at, cfg.polling.queue_seconds

        tick = 0
        while not envio.enviados:
            agora_do_tick = entrada + timedelta(seconds=passo * (tick + fase))
            await p.dispatcher.tick(agora_do_tick)
            tick += 1

        assert (agora_do_tick - entrada).total_seconds() <= 50
