"""Contrato do dispatch: todo alerta decidido para notificar deixa registro,
inclusive quando ninguem recebe (o modo de falha que faz um monitor virar mobilia)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from agent_nio_monitor.agent import Agent
from agent_nio_monitor.config.loader import load
from agent_nio_monitor.notify.mailer import DispatchResult
from agent_nio_monitor.store.repository import EventRecord
from agent_nio_monitor.store.memory_repository import InMemoryRepository


class _NotificadorFixo:
    """Substitui o `Notifier` real.

    Sem isto o teste faz POST no webhook de PRODUCAO
    (`http://192.168.0.160:5678/webhook/...`). Em isolado passa rapido; na
    suite completa a latencia de rede varia e o teste fica instavel -- foi
    exatamente o que aconteceu. Teste que depende da rede da empresa nao e
    teste.
    """

    def __init__(self, status: str = "sent") -> None:
        self.status = status
        self.chamadas: list[int] = []

    async def send(self, recipients, subject, body, html, n8n_up, severity, metadata=None):  # noqa: ANN001, ANN003
        self.chamadas.append(len(recipients))
        return DispatchResult(status=self.status, channel="teste")


class _AnalistaFixo:
    async def analyze(self, contexto: dict) -> tuple[None, str]:
        return None, "fallback"


def _evento_de_execucao() -> EventRecord:
    return EventRecord(
        id=uuid4(),
        ts=datetime(2026, 10, 13, 12, 0, tzinfo=UTC),
        source="n8n",
        event_type="execution_failed",
        severity="high",
        title="wf falhou 1x",
        resource_id="wf1",
        detail={"workflow_nome": "wf", "ocorrencias": 1, "template": "execution_failed"},
    )


async def test_projeto_desconhecido_agora_chega_alguem(monkeypatch: pytest.MonkeyPatch) -> None:
    """O que a D1 mudou: `people.yaml: global` deixou de estar vazio.

    Antes, um evento sem `tenant_id` resolvia em `Resolution(emails=[])`,
    o `mailer` devolvia `failed` e o alerta MORRIA. Com o fallback global
    preenchido, ele sai. Este teste e a prova de que a entrega real aconteceu
    -- a fiação estava pronta desde a Fase 1, mas sem valor ela era inerte.
    """
    monkeypatch.setenv("N8N_API_KEY", "k")
    from agent_nio_monitor.notify.people import global_fallback

    repo = InMemoryRepository()
    agent = Agent(load(), repo)
    agent.analyst = _AnalistaFixo()
    agent.notifier = _NotificadorFixo()
    try:
        resultado = await agent.dispatch(_evento_de_execucao())

        assert resultado.status == "sent", (
            f"o alerta sem projeto conhecido nao saiu: {resultado}. "
            "O fallback global tem que estar preenchido em people.yaml."
        )
        assert global_fallback(), "a D1 nao foi aplicada"
        assert agent.health.alertas_sem_destinatario == 0
        assert agent.notifier.chamadas, "nenhum envio foi tentado"
    finally:
        await agent.llm.aclose()
        await agent.n8n.aclose()


async def test_alerta_sem_destinatario_e_registrado_como_falha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O caminho de zero destinatario continua existindo -- e precisa de teste.

    Ele e alcançavel se `people.yaml: global` for esvaziado de novo (o que a
    integridade do e-mail nao pega). Aqui o fallback e forcado vazio para
    exercitar o caminho, sem depender do conteudo do YAML de teste.
    """
    monkeypatch.setenv("N8N_API_KEY", "k")
    repo = InMemoryRepository()
    agent = Agent(load(), repo)
    agent.analyst = _AnalistaFixo()
    agent.resolver.global_fallback = []
    try:
        resultado = await agent.dispatch(_evento_de_execucao())

        assert resultado.status == "failed"
        assert len(repo.notifications) == 1
        assert repo.notifications[0]["status"] == "failed"
    finally:
        await agent.llm.aclose()
        await agent.notifier.aclose()
        await agent.n8n.aclose()


async def test_alerta_perdido_e_visivel_no_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ser `failed` no banco nao basta: nada disso chega ao `/healthz`, que e o
    que o dead-man's switch do runbook consulta. Sem o contador, um alerta
    perdido por falta de destinatario e indistinguivel de um agente saudavel."""
    monkeypatch.setenv("N8N_API_KEY", "k")
    agent = Agent(load(), InMemoryRepository())
    agent.analyst = _AnalistaFixo()
    agent.resolver.global_fallback = []
    try:
        assert agent.health.alertas_sem_destinatario == 0
        await agent.dispatch(_evento_de_execucao())

        assert agent.health.alertas_sem_destinatario == 1
        assert agent.health.snapshot(max_stale=timedelta(minutes=10))["alertas_sem_destinatario"] == 1
    finally:
        await agent.llm.aclose()
        await agent.notifier.aclose()
        await agent.n8n.aclose()


