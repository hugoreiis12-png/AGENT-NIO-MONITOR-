"""Calculos de CPU/memoria e deteccao de candidato a restart-loop."""

from __future__ import annotations

import pytest

from agent_nio_monitor.sources.portainer.metrics import (
    cpu_do_host_pct,
    eh_candidato_a_loop,
    memoria_pct,
    totais_de_cpu,
)


class TestCpu:
    def test_media_entre_duas_coletas_como_fatia_do_host(self) -> None:
        assert cpu_do_host_pct((1_000, 10_000), (3_000, 20_000)) == 20.0

    def test_contador_que_voltou_atras_nao_gera_numero(self) -> None:
        assert cpu_do_host_pct((5_000, 10_000), (1_000, 20_000)) is None

    def test_sem_variacao_do_host_nao_divide_por_zero(self) -> None:
        assert cpu_do_host_pct((1, 10), (2, 10)) is None

    def test_totais_ausentes_viram_none(self) -> None:
        assert totais_de_cpu({"cpu_stats": {}}) is None
        completo = {"cpu_stats": {"cpu_usage": {"total_usage": 5}, "system_cpu_usage": 9}}
        assert totais_de_cpu(completo) == (5, 9)


class TestMemoria:
    def test_desconta_cache_de_arquivo(self) -> None:
        stats = {"memory_stats": {"usage": 900, "limit": 1000, "stats": {"inactive_file": 400}}}
        assert memoria_pct(stats) == 50.0

    def test_cgroup_v1_usa_cache(self) -> None:
        stats = {"memory_stats": {"usage": 800, "limit": 1000, "stats": {"cache": 300}}}
        assert memoria_pct(stats) == 50.0

    def test_sem_limite_nao_inventa_percentual(self) -> None:
        assert memoria_pct({"memory_stats": {"usage": 5}}) is None


@pytest.mark.parametrize(
    ("row", "esperado"),
    [
        ({"State": "restarting", "Status": "Restarting (1) 5 seconds ago"}, True),
        ({"State": "running", "Status": "Up 12 seconds"}, True),
        ({"State": "running", "Status": "Up Less than a second"}, True),
        ({"State": "running", "Status": "Up 2 minutes"}, True),
        ({"State": "running", "Status": "Up 4 weeks"}, False),
        ({"State": "running", "Status": "Up 5 hours (healthy)"}, False),
        ({"State": "exited", "Status": "Exited (0) 4 weeks ago"}, False),
    ],
)
def test_candidato_a_loop(row: dict, esperado: bool) -> None:
    assert eh_candidato_a_loop(row) is esperado