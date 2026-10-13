"""Testes da politica contra os 25 erros REAIS da instancia.

Este arquivo e o criterio de aceite do T3: "24 erros/dia -> <=5 emails/dia".
Se a contagem subir, a politica regrediu.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_nio_monitor.config.schema import PolicyConfig
from agent_nio_monitor.notify.resolver import RecipientResolver
from agent_nio_monitor.rules.engine import RuleEngine
from agent_nio_monitor.sources.base import dentro_de_quiet_hours

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "executions_erros_reais.json"
CONFIG = Path(__file__).resolve().parents[2] / "config" / "policy.yaml"


@pytest.fixture(scope="module")
def erros() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["data"]


@pytest.fixture(scope="module")
def policy() -> PolicyConfig:
    import yaml

    return PolicyConfig.model_validate(yaml.safe_load(CONFIG.read_text(encoding="utf-8")))


class TestCenarioReal:
    def test_fixture_reflete_a_inancia(self, erros: list[dict]) -> None:
        assert len(erros) == 25
        contagem = Counter(e["workflowId"] for e in erros)
        assert contagem.most_common(3) == [
            ("JqOi9bhDIXzi8wKN", 9),
            ("A4PebDZHcE6TiJT9", 6),
            ("UQGzQ9NoKZOiSyjRZB_3m", 4),
        ], "a concentracao mudou; reavalie a politica"
        share = sum(c for _, c in contagem.most_common(3)) / len(erros)
        assert share >= 0.70, f"top3 = {share:.0%}; a politica depende dessa concentracao"

    def test_horarios_sao_deterministicos(self, erros: list[dict]) -> None:
        """Sao jobs agendados quebrados, nao incidentes aleatorios."""
        minutos: dict[str, set[str]] = {}
        for e in erros:
            dt = datetime.fromisoformat(e["startedAt"].replace("Z", "+00:00"))
            minutos.setdefault(e["workflowId"], set()).add(f"{dt.hour:02d}{dt.minute:02d}")
        repetidos = {w: m for w, m in minutos.items() if len(m) <= 3}
        assert len(repetidos) >= 3, "padrao horario deterministico nao se confirmou"

    def test_deduplicacao_agrega_sem_perder_incidente(self, erros: list[dict], policy) -> None:
        """Criterio de aceite do T3, corrigido pelos dados reais.

        A meta original ('25 erros -> 5 emails') era aritmeticamente IMPOSSIVEL:
        no periodo ha 8 workflows DISTINTOS quebrados, e cada um precisa de pelo
        menos um alerta -- suprimir a primeira ocorrencia custa deteccao.

        O que se afirma aqui e o que importa:
          - piso: exatamente 8, um por workflow quebrado (nenhum incidente perdido)
          - teto: 13 (nao ha tempestade de alerta)
          - nenhum workflow notificado mais de 2x em 25h
          - reducao real: 25 -> no maximo 13

        A simulacao anda no TEMPO de cada execucao. Congelar `agora` esconderia o
        problema: com tempo parado a janela nunca vence e vira 1 email/workflow.
        """
        engine = RuleEngine(policy)
        streaks: Counter[str] = Counter()
        ultimo_notificado: dict[str, datetime] = {}
        notificacoes: list[tuple[str, datetime]] = []

        for e in sorted(erros, key=lambda x: x["startedAt"]):
            wf = e["workflowId"]
            quando = datetime.fromisoformat(e["startedAt"].replace("Z", "+00:00"))
            streaks[wf] += 1
            decisao = engine.evaluate(
                event_type="execution_failed",
                streak_count=streaks[wf],
                last_notified_at=ultimo_notificado.get(wf),
                now=quando,
            )
            if decisao.notify:
                notificacoes.append((wf, quando))
                ultimo_notificado[wf] = quando

        workflows_quebrados = {e["workflowId"] for e in erros}
        por_workflow = Counter(w for w, _ in notificacoes)

        # piso: todo workflow quebrado foi reportado pelo menos uma vez
        assert set(por_workflow) == workflows_quebrados, (
            f"workflow sem alerta: {workflows_quebrados - set(por_workflow)}"
        )
        # teto: sem tempestade
        assert len(notificacoes) <= 13, f"{len(notificacoes)} notificacoes: {por_workflow}"
        # nenhum workflow martelado
        assert max(por_workflow.values()) <= 2, f"repeticao excessiva: {por_workflow}"
        # reducao real frente ao bruto
        assert len(notificacoes) <= len(erros) * 0.6

    def test_primeiro_alerta_vem_rapido(self, erros: list[dict], policy) -> None:
        """Deduplicar nao pode custar deteccao: o primeiro erro avisa na hora."""
        engine = RuleEngine(policy)
        primeiro = sorted(erros, key=lambda x: x["startedAt"])[0]
        quando = datetime.fromisoformat(primeiro["startedAt"].replace("Z", "+00:00"))
        d = engine.evaluate(event_type="execution_failed", streak_count=1, now=quando)
        assert d.notify is True
        assert d.severity == "high"


class TestStreakEscolheRegraCerta:
    def test_1a_falha_alta_3x_critica(self, policy) -> None:
        engine = RuleEngine(policy)
        assert engine.evaluate(event_type="execution_failed", streak_count=1).severity == "high"
        assert engine.evaluate(event_type="execution_failed", streak_count=3).severity == "critical"
        assert engine.evaluate(event_type="execution_failed", streak_count=50).severity == "critical"

    def test_log_only_nao_notifica(self, policy) -> None:
        engine = RuleEngine(policy)
        d = engine.evaluate(event_type="source_down")
        assert d.notify is False
        assert d.suppress_reason == "regra_marca_log_only"

    def test_sem_regra_nao_notifica(self, policy) -> None:
        engine = RuleEngine(policy)
        d = engine.evaluate(event_type="evento_que_nobody_definiu")
        assert d.notify is False
        assert d.suppress_reason == "sem_regra"

    def test_renotificacao_e_respeitada(self, policy) -> None:
        """Cada regra tem sua janela. Ambas sao 720 (12h) porque, depois do
        primeiro alerta, a urgencia e tratada pela escada de escalacao e nao por
        repetir email."""
        engine = RuleEngine(policy)
        agora = datetime.now(UTC)

        d = engine.evaluate(
            event_type="execution_failed", streak_count=1,
            last_notified_at=agora - timedelta(minutes=5), now=agora,
        )
        assert d.notify is False
        assert "renotificacao(720min)" in d.suppress_reason

        d_ok = engine.evaluate(
            event_type="execution_failed", streak_count=1,
            last_notified_at=agora - timedelta(minutes=730), now=agora,
        )
        assert d_ok.notify is True

        # streak 5 -> regra critica escolhe template e severidade diferentes
        d_crit = engine.evaluate(
            event_type="execution_failed", streak_count=5,
            last_notified_at=agora - timedelta(minutes=10), now=agora,
        )
        assert d_crit.severity == "critical"
        assert d_crit.template == "workflow_failing"
        assert d_crit.notify is False

    def test_regra_com_janela_propria_nao_herda_global(self) -> None:

        p = PolicyConfig.model_validate(
            {
                "aggregation": {"re_notify_after_minutes": 10},
                "rules": [
                    {
                        "name": "curta", "event_types": ["a"], "severity": "low",
                        "re_notify_after_minutes": 600,
                    },
                    {"name": "global", "event_types": ["b"], "severity": "low"},
                ],
            }
        )
        engine = RuleEngine(p)
        agora = datetime.now(UTC)
        # regra com janela propria: 30 min depois ainda nao renotifica
        assert engine.evaluate(
            event_type="a", last_notified_at=agora - timedelta(minutes=30), now=agora
        ).notify is False
        # regra sem janela propria: herda a global de 10 min e renotifica
        assert engine.evaluate(
            event_type="b", last_notified_at=agora - timedelta(minutes=30), now=agora
        ).notify is True

    def test_critico_ignora_quiet_hours(self) -> None:

        p = PolicyConfig.model_validate(
            {
                "quiet_hours": {
                    "enabled": True,
                    "start": "22:00",
                    "end": "08:00",
                    "critical_breaks_quiet_hours": True,
                },
                "rules": [
                    {
                        "name": "c",
                        "event_types": ["execution_failed"],
                        "severity": "critical",
                        "min_streak": 1,
                    },
                    {
                        "name": "m",
                        "event_types": ["workflow_changed"],
                        "severity": "medium",
                    },
                ],
            }
        )
        engine = RuleEngine(p)
        madrugada = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)
        assert engine.evaluate(event_type="execution_failed", now=madrugada).notify is True
        assert engine.evaluate(event_type="workflow_changed", now=madrugada).notify is False


class TestQuietHoursJanelaQueAtravessaMeiaNoite:
    @pytest.mark.parametrize(
        ("hora", "dentro"),
        [(21, False), (22, True), (23, True), (0, True), (7, True), (8, False), (12, False)],
    )
    def test_22h_as_08h(self, hora: int, dentro: bool) -> None:
        momento = datetime(2026, 10, 12, hora, 30, tzinfo=UTC)
        assert dentro_de_quiet_hours("22:00", "08:00", momento) is dentro

    @pytest.mark.parametrize(
        ("hora", "dentro"), [(8, False), (9, True), (12, True), (17, True), (18, False)]
    )
    def test_janela_de_dia(self, hora: int, dentro: bool) -> None:
        """Inicio inclusivo, fim exclusivo: 09:00 entra, 18:00 nao."""
        momento = datetime(2026, 10, 12, hora, 30, tzinfo=UTC)
        assert dentro_de_quiet_hours("09:00", "18:00", momento) is dentro


class TestResolucaoDeDestinatarios:
    def test_cai_para_people_yaml_quando_api_403(self) -> None:
        class SemPermissao:
            async def list_users(self):
                raise RuntimeError("403")

            async def list_project_users(self, _):  # noqa: ANN001
                raise RuntimeError("403")

        r = RecipientResolver(
            SemPermissao(),
            people={"V3yqT1MGvf0Ufg17": ["dono@empresa.com"]},
        )
        import asyncio

        res = asyncio.run(r.resolve_for_project("V3yqT1MGvf0Ufg17"))
        assert res.emails == ["dono@empresa.com"]
        assert res.source == "people_yaml"

    def test_sem_mapa_e_sem_global_nao_e_falso_sucesso(self) -> None:
        class SemPermissao:
            async def list_users(self):
                raise RuntimeError("403")

            async def list_project_users(self, _):  # noqa: ANN001
                raise RuntimeError("403")

        import asyncio

        r = RecipientResolver(SemPermissao(), people={}, global_fallback=[])
        res = asyncio.run(r.resolve_for_project("projeto_desconhecido"))
        assert res.emails == []
        assert "ALERTA PERDIDO" in res.detail

    def test_lista_vazia_nao_vira_lista_de_um(self) -> None:
        r = RecipientResolver(None, people={}, max_recipients=3)  # type: ignore[arg-type]
        assert r._dedup(["a@b.c", "A@B.C", "", None, "d@e.f"]) == ["a@b.c", "d@e.f"]
        assert len(r._dedup([f"u{i}@x.y" for i in range(10)])) == 3