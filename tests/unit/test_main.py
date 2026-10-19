"""Entrypoint e scheduler: o laco de cada job, a agenda e os comandos da CLI.

O scheduler e o que mantem o agente vivo -- um job que levanta nao pode parar os
outros, e `stop` tem que encerrar todos. Sem estes testes `main.py` ficava em 29%.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
import structlog

from agent_nio_monitor import main as main_mod
from agent_nio_monitor.capabilities import Capability, CapabilityReport
from agent_nio_monitor.config.loader import ConfigError

INTERVALO_CURTO = 0.01
JOBS_DA_AGENDA = 11


class _AgenteFalso:
    """So os metodos que a agenda chama; cada um conta as proprias chamadas."""

    def __init__(self, stop: asyncio.Event, chamadas_ate_parar: int = 2) -> None:
        intervalos = dict.fromkeys(
            ["execution_errors_seconds", "workflow_full_seconds", "container_state_seconds",
             "stack_changes_seconds", "container_stats_seconds", "queue_seconds"],
            INTERVALO_CURTO,
        )
        self.cfg = SimpleNamespace(polling=SimpleNamespace(**intervalos))
        self.chamadas: dict[str, int] = {}
        self.sondou = False
        self._stop, self._limite = stop, chamadas_ate_parar

    async def _conta(self, nome: str) -> int:
        self.chamadas[nome] = self.chamadas.get(nome, 0) + 1
        if len(self.chamadas) == JOBS_DA_AGENDA and min(self.chamadas.values()) >= self._limite:
            self._stop.set()
        return 0

    async def run_once(self) -> int:
        return await self._conta("run_once")

    async def check_workflows_full(self) -> int:
        return await self._conta("check_workflows_full")

    async def check_credentials(self) -> int:
        return await self._conta("check_credentials")

    async def check_containers(self) -> int:
        return await self._conta("check_containers")

    async def check_stacks(self) -> int:
        return await self._conta("check_stacks")

    async def check_container_resources(self) -> int:
        return await self._conta("check_container_resources")

    async def flush_queue(self) -> int:
        return await self._conta("flush_queue")

    async def check_power_automate(self) -> int:
        return await self._conta("check_power_automate")

    async def check_execution_paths(self) -> int:
        return await self._conta("check_execution_paths")

    async def check_container_logs(self) -> int:
        return await self._conta("check_container_logs")

    async def maintain_partitions(self) -> int:
        return await self._conta("maintain_partitions")

    async def run_capabilities(self) -> dict[str, int]:
        self.sondou = True
        return {}


class TestLacoDoJob:
    async def test_repete_ate_o_stop(self) -> None:
        stop, chamadas = asyncio.Event(), []

        async def job() -> None:
            chamadas.append(1)
            if len(chamadas) == 3:
                stop.set()

        await asyncio.wait_for(main_mod._laco_do_job("t", INTERVALO_CURTO, job, stop), 2)
        assert len(chamadas) == 3

    async def test_job_que_levanta_nao_para_o_laco(self) -> None:
        stop, chamadas = asyncio.Event(), []

        async def job() -> None:
            chamadas.append(1)
            if len(chamadas) == 2:
                stop.set()
            raise RuntimeError("falha de um ciclo")

        await asyncio.wait_for(main_mod._laco_do_job("t", INTERVALO_CURTO, job, stop), 2)
        assert len(chamadas) == 2, "o erro de um ciclo nao pode matar o job"

    async def test_job_mais_lento_que_o_intervalo_roda_de_novo_sem_esperar(self) -> None:
        stop, chamadas = asyncio.Event(), []

        async def job() -> None:
            chamadas.append(1)
            await asyncio.sleep(INTERVALO_CURTO * 2)
            if len(chamadas) == 2:
                stop.set()

        await asyncio.wait_for(main_mod._laco_do_job("t", INTERVALO_CURTO, job, stop), 2)
        assert len(chamadas) == 2


class TestAgenda:
    async def test_todos_os_jobs_rodam_e_a_sonda_roda_uma_vez(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(main_mod, "MANUTENCAO_SEGUNDOS", INTERVALO_CURTO)
        stop = asyncio.Event()
        agente = _AgenteFalso(stop)

        await asyncio.wait_for(main_mod._agenda(agente, stop), 5)

        assert len(agente.chamadas) == JOBS_DA_AGENDA, f"job fora da agenda: {agente.chamadas}"
        assert agente.sondou, "a sonda de capacidade (D2) tem que rodar no boot"


class TestHandlersDeSinal:
    async def test_nao_derruba_o_boot_em_nenhuma_plataforma(self) -> None:
        main_mod._instala_handlers_de_sinal(asyncio.Event())


def _cfg_sem_power_automate() -> SimpleNamespace:
    return SimpleNamespace(active=SimpleNamespace(powerautomate=None))


class TestDoctor:
    async def test_power_automate_configurado_aparece_no_doctor(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # noqa: ANN001
        async def _sonda(pa):  # noqa: ANN001, ANN202
            return "OK   acesso (WhoAmI)"

        monkeypatch.setattr(main_mod, "sondar_do_config", _sonda)
        monkeypatch.setattr(main_mod, "fontes_do_config", lambda cfg: {})
        cfg = SimpleNamespace(active=SimpleNamespace(powerautomate=object()))

        await main_mod.cmd_doctor(cfg)
        saida = capsys.readouterr().out
        assert "=== POWER AUTOMATE ===" in saida and "WhoAmI" in saida

    async def test_sem_credencial_devolve_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(main_mod, "fontes_do_config", lambda cfg: {})
        assert await main_mod.cmd_doctor(_cfg_sem_power_automate()) == 2

    async def test_imprime_a_tabela_de_cada_fonte(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # noqa: ANN001
        relatorio = CapabilityReport(items=[
            Capability("n8n", "GET", "/healthz", "health", True, True, 200),
        ])

        async def _detect(fontes):  # noqa: ANN001, ANN202
            return {"n8n": relatorio}

        monkeypatch.setattr(main_mod, "fontes_do_config", lambda cfg: {"n8n": {}})
        monkeypatch.setattr(main_mod, "detect", _detect)

        assert await main_mod.cmd_doctor(_cfg_sem_power_automate()) == 0
        saida = capsys.readouterr().out
        assert "=== N8N ===" in saida and "/healthz" in saida


class TestMain:
    def test_config_invalida_devolve_2(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # noqa: ANN001
        def _falha() -> None:
            raise ConfigError("faltou N8N_API_KEY")

        monkeypatch.setattr(sys, "argv", ["agent-nio-monitor", "once"])
        monkeypatch.setattr(main_mod, "load", _falha)

        assert main_mod.main() == 2
        assert "ERRO DE CONFIG" in capsys.readouterr().err

    def test_once_roda_uma_passada(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # noqa: ANN001
        class _Agente:
            async def run_once(self) -> dict[str, int]:
                return {"eventos": 0}

        monkeypatch.setattr(sys, "argv", ["agent-nio-monitor", "once"])
        monkeypatch.setattr(main_mod, "load", lambda: SimpleNamespace(log_level="WARNING"))
        monkeypatch.setattr(main_mod, "setup_logging", lambda level: None)
        monkeypatch.setattr(main_mod, "build", lambda cfg: _Agente())

        assert main_mod.main() == 0
        assert "eventos" in capsys.readouterr().out


class TestLogging:
    def test_carimbo_tem_o_deslocamento_de_brasilia(self) -> None:
        evento = main_mod.local_timestamper()(None, "info", {})
        assert evento["timestamp"].endswith("-03:00")

    def test_setup_logging_emite_json(self, capsys) -> None:  # noqa: ANN001
        main_mod.setup_logging("INFO")
        try:
            structlog.get_logger().info("teste_de_log", campo=1)
            linha = capsys.readouterr().out.strip().splitlines()[-1]
            assert json.loads(linha)["event"] == "teste_de_log"
        finally:
            structlog.reset_defaults()
