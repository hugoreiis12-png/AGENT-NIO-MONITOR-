"""Logs saem no horario de Brasilia, com o deslocamento explicito."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_nio_monitor.main import local_timestamper


def _carimbo(timezone: str = "America/Sao_Paulo") -> str:
    return local_timestamper(timezone)(None, "info", {"event": "x"})["timestamp"]


def test_carimbo_tem_o_deslocamento_de_brasilia() -> None:
    assert _carimbo().endswith("-03:00")


def test_o_instante_continua_correto() -> None:
    lido = datetime.fromisoformat(_carimbo())
    assert abs(datetime.now(UTC) - lido) < timedelta(seconds=5)


def test_o_horario_bate_com_o_relogio_de_brasilia() -> None:
    lido = datetime.fromisoformat(_carimbo())
    assert (lido.hour - datetime.now(UTC).hour) % 24 == 21  # UTC-3 == +21 mod 24


def test_outro_fuso_e_respeitado() -> None:
    assert _carimbo("UTC").endswith("+00:00")


def test_preserva_o_restante_do_evento() -> None:
    saida = local_timestamper()(None, "info", {"event": "x", "k": 1})
    assert saida["event"] == "x" and saida["k"] == 1
