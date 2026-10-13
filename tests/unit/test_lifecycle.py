"""Container removido de verdade x recriado; loop de reinicio dentro da janela."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_nio_monitor.sources.portainer.lifecycle import LifecycleTracker

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _tracker() -> LifecycleTracker:
    return LifecycleTracker(loop_count=3, loop_window=timedelta(minutes=10))


def _nomes(*nomes: str) -> dict:
    return {n: {"stack": "s"} for n in nomes}


class TestRemocao:
    def test_primeira_passada_so_forma_a_linha_de_base(self) -> None:
        assert _tracker().removidos(1, _nomes("a", "b")) == []

    def test_nome_ausente_so_e_confirmado_na_segunda_coleta(self) -> None:
        t = _tracker()
        t.removidos(1, _nomes("a", "b"))
        assert t.removidos(1, _nomes("a")) == []
        assert [n for n, _ in t.removidos(1, _nomes("a"))] == ["b"]

    def test_reaparecer_na_coleta_seguinte_cancela_a_ausencia(self) -> None:
        t = _tracker()
        t.removidos(1, _nomes("a", "b"))
        t.removidos(1, _nomes("a"))
        t.removidos(1, _nomes("a", "b"))
        assert t.removidos(1, _nomes("a")) == []

    def test_removido_nao_e_reportado_de_novo(self) -> None:
        t = _tracker()
        t.removidos(1, _nomes("a", "b"))
        t.removidos(1, _nomes("a"))
        t.removidos(1, _nomes("a"))
        assert t.removidos(1, _nomes("a")) == []

    def test_endpoints_sao_independentes(self) -> None:
        t = _tracker()
        t.removidos(1, _nomes("a"))
        assert t.removidos(2, _nomes()) == []


class TestLoopDeReinicio:
    def test_tres_reinicios_na_janela_alertam(self) -> None:
        t = _tracker()
        assert t.reinicios_na_janela("c", 5, T0) is None
        assert t.reinicios_na_janela("c", 7, T0 + timedelta(minutes=2)) is None
        assert t.reinicios_na_janela("c", 8, T0 + timedelta(minutes=4)) == 3

    def test_reinicios_espalhados_alem_da_janela_nao_alertam(self) -> None:
        t = _tracker()
        t.reinicios_na_janela("c", 0, T0)
        assert t.reinicios_na_janela("c", 3, T0 + timedelta(minutes=30)) is None

    def test_nao_repete_o_alerta_dentro_da_mesma_janela(self) -> None:
        t = _tracker()
        t.reinicios_na_janela("c", 0, T0)
        assert t.reinicios_na_janela("c", 4, T0 + timedelta(minutes=1)) == 4
        assert t.reinicios_na_janela("c", 6, T0 + timedelta(minutes=2)) is None