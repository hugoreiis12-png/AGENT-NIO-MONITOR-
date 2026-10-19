"""Particoes mantidas em runtime: sem isto o primeiro mes sem particao derruba todo INSERT."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_nio_monitor.config.loader import load
from agent_nio_monitor.config.schema import DatabaseConfig
from agent_nio_monitor.main import MANUTENCAO_SEGUNDOS
from agent_nio_monitor.store.maintenance import MESES_A_FRENTE, PartitionMaintenance


class BancoFalso:
    def __init__(self) -> None:
        self.garantidas: list[int] = []
        self.retencoes: list[int] = []

    def ensure_partitions(self, months_ahead: int = MESES_A_FRENTE) -> None:
        self.garantidas.append(months_ahead)

    def drop_partitions_older_than(self, days: int) -> list[tuple[str, str]]:
        self.retencoes.append(days)
        return [("event", "event_2026_01")]


async def test_cada_rodada_garante_os_meses_a_frente() -> None:
    banco = BancoFalso()
    manutencao = PartitionMaintenance(banco, retention_days=None)
    await manutencao.run()
    await manutencao.run()
    assert banco.garantidas == [MESES_A_FRENTE, MESES_A_FRENTE]


async def test_sem_retencao_configurada_nada_e_apagado() -> None:
    banco = BancoFalso()
    assert await PartitionMaintenance(banco, retention_days=None).run() == 0
    assert banco.retencoes == []


async def test_retencao_configurada_remove_e_conta() -> None:
    banco = BancoFalso()
    assert await PartitionMaintenance(banco, retention_days=90).run() == 1
    assert banco.retencoes == [90]


def test_retencao_curta_demais_e_recusada_no_boot() -> None:
    with pytest.raises(ValidationError):
        DatabaseConfig(dsn="postgresql://x", retention_days=30)


def test_roda_mais_de_uma_vez_por_dia() -> None:
    """Uma vez por dia pode cair depois da virada do mes; o job tem folga."""
    assert MANUTENCAO_SEGUNDOS < 24 * 3600


async def test_agent_sem_banco_nao_mantem_nada() -> None:
    from agent_nio_monitor.agent import Agent
    from agent_nio_monitor.store.memory_repository import InMemoryRepository

    agente = Agent(load(), InMemoryRepository())
    try:
        assert await agente.maintain_partitions() == 0
    finally:
        await agente.llm.aclose()
        await agente.notifier.aclose()
        await agente.n8n.aclose()
