"""Semantica do streak: a ordem de chegada nao pode mudar o resultado.

O bug que este arquivo existe para impedir
---------------------------------------------
`InMemoryRepository.bump_streak` fazia:

    if s is None or s.first_at is None or at > s.last_at:
        s = Streak(..., count=0, ...)      # <- ZERAVA

Como o n8n devolve execucoes da mais nova para a mais antiga, e como um workflow
que comeca a falhar gera erros com timestamp CRESCENTE ao longo das horas, o
contador voltava a 1 a cada falha. Medido antes da correcao:

    9 falhas em horarios crescentes: 1, 1, 1, 1, 1, 1, 1, 1, 1

Consequencia: a regra `min_streak: 3` (severity critical) NUNCA disparava. O
alerta mais valioso do sistema inteiro estava morto -- e os testes passavam,
porque a fixture estava em ordem DECRESCENTE, onde o bug nao se manifesta.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_nio_monitor.store.memory_repository import InMemoryRepository

BASE = datetime(2026, 10, 12, 16, 30, tzinfo=UTC)


def _timestamps(n: int, ordem: str, passo_min: int = 37) -> list[datetime]:
    """N timestamps distintos (ou nao) conforme a ordem de chegada."""
    if ordem == "crescente":
        return [BASE + timedelta(minutes=passo_min * i) for i in range(n)]
    if ordem == "decrescente":
        return [BASE + timedelta(minutes=passo_min * (n - 1 - i)) for i in range(n)]
    if ordem == "igual":
        return [BASE] * n
    if ordem == "embaralhado":
        base = [BASE + timedelta(minutes=passo_min * i) for i in range(n)]
        # interleave: primeiro, ultimo, segundo, penultimo...
        return base[::2] + base[1::2][::-1]
    raise ValueError(ordem)  # pragma: no cover - erro do teste


def _repo_com(n: int, *, ordem: str, passo_min: int = 37) -> InMemoryRepository:
    repo = InMemoryRepository()
    for t in _timestamps(n, ordem, passo_min):
        repo.bump_streak("wf1", t, "proj1", "Processa Checkout")
    return repo


ORDENS = ["crescente", "decrescente", "igual", "embaralhado"]


class TestStreakNaoDependeDaOrdem:
    @pytest.mark.parametrize("ordem", ORDENS)
    def test_contador_iguais_em_qualquer_ordem(self, ordem: str) -> None:
        repo = _repo_com(9, ordem=ordem)
        assert repo.get_streak("wf1").count == 9, (
            f"ordem '{ordem}' produziu count={repo.get_streak('wf1').count}, esperado 9"
        )

    @pytest.mark.parametrize("ordem", ORDENS)
    def test_primeira_e_ultima_sao_as_extremas(self, ordem: str) -> None:
        """A janela tem de ser a mesma, venha a falha na ordem que vier --
        comparada contra os extremos REAIS desses timestamps, nao contra uma
        suposicao de espalhamento."""
        ts = _timestamps(9, ordem)
        repo = _repo_com(9, ordem=ordem)
        s = repo.get_streak("wf1")
        assert s.first_at == min(ts)
        assert s.last_at == max(ts)


class TestCasoRealQueQuebrava:
    def test_9_falhas_crescentes_atinge_streak_critico(self) -> None:
        """Cenario literal do teu ambiente: JqOi9bhDIXzi8wKN falhou 9x em 25h,
        sempre no mesmo horario."""
        repo = _repo_com(9, ordem="crescente")
        assert repo.get_streak("wf1").count == 9

    def test_regra_critical_finalmente_dispara(self) -> None:
        import yaml
        from pathlib import Path

        from agent_nio_monitor.config.schema import PolicyConfig
        from agent_nio_monitor.rules.engine import RuleEngine

        policy = PolicyConfig.model_validate(
            yaml.safe_load(
                (Path(__file__).resolve().parents[2] / "config" / "policy.yaml")
                .read_text(encoding="utf-8")
            )
        )
        engine = RuleEngine(policy)
        repo = _repo_com(9, ordem="crescente")
        count = repo.get_streak("wf1").count
        decisao = engine.evaluate(event_type="execution_failed", streak_count=count)
        assert decisao.severity == "critical", (
            f"com count={count} a severidade deveria ser critical, veio {decisao.severity}"
        )
        assert decisao.template == "workflow_failing"


class TestAcumulacaoIncremental:
    def test_ciclos_de_polling_nao_zeram(self) -> None:
        """Cada ciclo de 60s recebe uma pagina. O total tem de crescer
        monotonicamente ao longo dos ciclos."""
        repo = InMemoryRepository()
        counts = []
        for i in range(9):
            t = BASE + timedelta(minutes=37 * i)
            counts.append(repo.bump_streak("wf1", t, "p", "N").count)
        assert counts == [1, 2, 3, 4, 5, 6, 7, 8, 9]

    def test_workflows_independentes_nao_conflui(self) -> None:
        repo = InMemoryRepository()
        for i in range(5):
            repo.bump_streak("a", BASE + timedelta(minutes=i), "p", "A")
        for i in range(2):
            repo.bump_streak("b", BASE + timedelta(minutes=i), "p", "B")
        assert repo.get_streak("a").count == 5
        assert repo.get_streak("b").count == 2

    def test_reinsert_do_mesmo_id_no_collector_nao_dobra_streak(self) -> None:
        """Deduplicacao e por `execution_id`. Sem ela, reprocessar a mesma pagina
        infla o contador e dispara alerta falso de 'falha repetida'."""
        repo = InMemoryRepository()
        ids = [1, 2, 3]
        t = BASE
        vistos: set[int] = set()
        for exec_id in ids:
            if exec_id in vistos:
                continue
            vistos.add(exec_id)
            repo.bump_streak("wf1", t + timedelta(minutes=exec_id), "p", "N")
        # segunda passada, mesma pagina
        for exec_id in ids:
            if exec_id in vistos:
                continue
            vistos.add(exec_id)
            repo.bump_streak("wf1", t, "p", "N")
        assert repo.get_streak("wf1").count == 3


class TestCamposDoStreak:
    def test_tenant_e_nome_sao_campos_reais(self) -> None:
        """Antes eram atributos injetados com `# type: ignore`; o Postgres
        devolvia um objeto que nao os tinha, e o nome sumia em producao."""
        from agent_nio_monitor.store.repository import Streak

        campos = Streak.__dataclass_fields__
        assert "tenant_id" in campos
        assert "workflow_name" in campos

    def test_get_streak_devolve_os_campos(self) -> None:
        repo = InMemoryRepository()
        repo.bump_streak("wf1", BASE, "proj-xyz", "Meu Workflow")
        s = repo.get_streak("wf1")
        assert s.tenant_id == "proj-xyz"
        assert s.workflow_name == "Meu Workflow"

    def test_nome_preservado_quando_chega_none(self) -> None:
        repo = InMemoryRepository()
        repo.bump_streak("wf1", BASE, "p", "Nome Original")
        repo.bump_streak("wf1", BASE + timedelta(minutes=1), None, None)
        assert repo.get_streak("wf1").workflow_name == "Nome Original"

    def test_streak_limpo_aceita_os_campos(self) -> None:
        from agent_nio_monitor.store.repository import Streak

        s = Streak(
            workflow_id="w", count=1, first_at=BASE, last_at=BASE,
            tenant_id="p", workflow_name="N",
        )
        assert s.tenant_id == "p" and s.workflow_name == "N"


class TestNotificacaoMarca:
    def test_mark_streak_notified_e_persistido(self) -> None:
        repo = InMemoryRepository()
        repo.bump_streak("wf1", BASE, "p", "N")
        assert repo.get_streak("wf1").last_notified_at is None
        repo.mark_streak_notified("wf1", BASE)
        assert repo.get_streak("wf1").last_notified_at == BASE

    def test_resolve_streak_apaga(self) -> None:
        repo = InMemoryRepository()
        repo.bump_streak("wf1", BASE, "p", "N")
        repo.resolve_streak("wf1", BASE)
        assert repo.get_streak("wf1") is None