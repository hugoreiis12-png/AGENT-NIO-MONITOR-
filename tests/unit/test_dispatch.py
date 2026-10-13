"""Contrato do dispatch: todo alerta decidido para notificar deixa registro,
inclusive quando ninguem recebe (o modo de falha que faz um monitor virar mobilia)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agent_nio_monitor.agent import Agent
from agent_nio_monitor.config.loader import load
from agent_nio_monitor.store.repository import EventRecord, InMemoryRepository


class _EscritorFixo:
    async def write(self, contexto: dict) -> tuple[str, str]:
        return "abertura", "fallback"


async def test_alerta_sem_destinatario_e_registrado_como_falha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("N8N_API_KEY", "k")
    repo = InMemoryRepository()
    agent = Agent(load(), repo)
    agent.writer = _EscritorFixo()
    evento = EventRecord(
        id=uuid4(),
        ts=datetime(2026, 10, 13, 12, 0, tzinfo=UTC),
        source="n8n",
        event_type="execution_failed",
        severity="high",
        title="wf falhou 1x",
        resource_id="wf1",
        detail={"workflow_nome": "wf", "ocorrencias": 1, "template": "execution_failed"},
    )

    resultado = await agent.dispatch(evento)

    assert resultado.status == "failed"
    assert len(repo.notifications) == 1
    assert repo.notifications[0]["status"] == "failed"
    await agent.llm.aclose()
    await agent.notifier.aclose()
    await agent.n8n.aclose()
