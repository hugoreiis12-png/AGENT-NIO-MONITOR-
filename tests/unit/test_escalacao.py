"""Escalacao: configurada, logica pronta, e NAO LIGADA.

`policy.yaml` declara `escalate_after_minutes: [15, 45]` em regras reais. A
classe `Escalator` existe, esta correta e tem teste -- e nenhum modulo a
instancia em producao. O ADR descreve degraus que nunca disparam.

Este arquivo existe para que essa conta fique ABERTA e visivel, e nao vire uma
das tres configuracoes mortas ja encontradas nesta fase (`reconciliation_gap`,
`save_capabilities`, `resolve_for_portainer_resource`). Todas tres estavam
assim: presentes na configuracao, com codigo de suporte, e nenhuma fa wired.

Se este arquivo falhar, ou a escalacao foi ligada (atualize o ADR e apague a
decisao registrada aqui) ou alguem adicionou degraus a uma regra sem decidir.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_nio_monitor.config.loader import load_policy
from agent_nio_monitor.notify.mailer import Escalator


def _regras_com_escalacao() -> dict[str, list[int]]:
    return {
        r.name: r.escalate_after_minutes
        for r in load_policy().rules
        if r.escalate_after_minutes
    }


class TestConfiguracaoExiste:
    def test_ha_regras_declarando_degraus(self) -> None:
        """Trava a premissa. Se ninguem declarar mais degraus, a escalacao vira
        delcada e este arquivo inteiro pode ser apagado."""
        regras = _regras_com_escalacao()
        assert regras, "nenhuma regra declara escalacao -- reveja a decisao registrada"
        for nome, degraus in regras.items():
            assert degraus == sorted(degraus), f"{nome}: degraus fora de ordem"
            assert min(degraus) > 0, f"{nome}: degrau nao pode ser 0"


class TestEstadoAtual:
    def test_escalator_nao_e_instanciado_em_producao(self) -> None:
        """A verificacao central deste arquivo.

        Varre `src/` procurando `Escalator(`. Se aparecer fora deste arquivo e
        do teste unitario da classe, a escalacao foi ligada -- e entao este
        arquivo esta desatualizado e o ADR precisa ser reescrito.
        """
        import ast
        from pathlib import Path

        raiz = Path(__file__).resolve().parents[2] / "src" / "agent_nio_monitor"
        uso_em_producao: list[str] = []
        for caminho in raiz.rglob("*.py"):
            if "__pycache__" in caminho.parts:
                continue
            arvore = ast.parse(caminho.read_text(encoding="utf-8"))
            for no in ast.walk(arvore):
                if (
                    isinstance(no, ast.Call)
                    and isinstance(no.func, ast.Name)
                    and no.func.id == "Escalator"
                ):
                    uso_em_producao.append(str(caminho.relative_to(raiz)))

        assert not uso_em_producao, (
            f"Escalator instanciado em {uso_em_producao}. A escalacao foi LIGADA: "
            "atualize o ADR, documente a migration 005 e apague a decisao de "
            "deixar para depois registrada em mailer.Escalator."
        )

    def test_degrau_quebrado_nao_e_reatribuido_para_o_primeiro(self) -> None:
        """A razao de `due()` devolver o MAIOR degrau vencido, e nao o menor.

        Devolver o menor re-dispararia o primeiro degrau para sempre quando a
        escalacao atrasa -- que e reenvio, nao escalacao, e e treino para
        ignorar alerta. Este teste e o que trava essa distincao."""
        esc = Escalator([15, 45])
        agora = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
        notificou = agora - timedelta(minutes=60)

        assert esc.due(notified_at=notificou, now=agora) == 45

    def test_sem_tempo_decorrido_nao_escala(self) -> None:
        esc = Escalator([15, 45])
        agora = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
        assert esc.due(notified_at=agora - timedelta(minutes=5), now=agora) is None

    def test_nunca_notificado_nao_escala(self) -> None:
        esc = Escalator([15, 45])
        agora = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
        assert esc.due(notified_at=None, now=agora) is None

    def test_proximo_degrau_avanca_sem_voltar(self) -> None:
        """Cada degrau dispara UMA vez e nao retrocede.

        Com 60 min decorridos os dois degraus venceram; o que muda entre as
        chamadas e `last_step`, que impede o degrau 15 de reaparecer depois de
        ja ter disparado -- seria o reenvio que a docstring diz que nao e
        escalacao."""
        esc = Escalator([15, 45])
        agora = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
        notificou = agora - timedelta(minutes=60)

        assert esc.next_step(None, now=agora, notified_at=notificou) == 15
        assert esc.next_step(15, now=agora, notified_at=notificou) == 45
        assert esc.next_step(45, now=agora, notified_at=notificou) is None

    def test_degrau_que_nao_venceu_ainda_nao_dispara(self) -> None:
        """20 min: venceu o 15, o 45 ainda nao. Disparar o 45 aqui seria
        escalar antes do prazo configurado."""
        esc = Escalator([15, 45])
        agora = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
        notificou = agora - timedelta(minutes=20)

        assert esc.next_step(15, now=agora, notified_at=notificou) is None