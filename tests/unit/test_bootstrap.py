"""Montagem do agente: com repositorio injetado nao toca no banco; sem DSN falha no boot."""

from __future__ import annotations

import pytest

from agent_nio_monitor.bootstrap import build, make_db
from agent_nio_monitor.config.loader import ConfigError, load
from agent_nio_monitor.store.memory_repository import InMemoryRepository


async def test_repositorio_injetado_dispensa_o_banco() -> None:
    repo = InMemoryRepository()
    agente = build(load(), repo)
    try:
        assert agente.repo is repo
    finally:
        await agente.llm.aclose()
        await agente.notifier.aclose()
        await agente.n8n.aclose()


def test_sem_dsn_falha_no_boot_com_mensagem_clara() -> None:
    cfg = load()
    cfg.database.dsn = "${DATABASE_DSN}"
    with pytest.raises(ConfigError, match="DATABASE_DSN ausente"):
        make_db(cfg)
