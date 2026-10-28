"""Coletor de RPA (fluxo de desktop) e o caminho ate o e-mail.

Formato das sessoes copiado do ambiente RPA-CONTAS-A-RECEBER (14/10/2026): status 8 =
Failed, 4 = Succeeded, rotulos via anotacao FormattedValue, dono por `owninguser`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import respx

from agent_nio_monitor.config.loader import load
from agent_nio_monitor.notify.resolution import ResolutionChecker
from agent_nio_monitor.rules.engine import RuleEngine
from agent_nio_monitor.sources.powerautomate.client import DataverseClient
from agent_nio_monitor.sources.powerautomate.desktop import DesktopFlowCollector, parse_sessao
from agent_nio_monitor.sources.powerautomate.events import TIPO, evento_de_falha_desktop
from agent_nio_monitor.store.memory_repository import InMemoryRepository

DV = "https://orgteste.crm2.dynamics.com"
SESSOES = f"{DV}/api/data/v9.2/flowsessions"
TOKEN = "https://login.microsoftonline.com/tenant/oauth2/v2.0/token"
FLUXO = "f-0001"
ROTULO = "@OData.Community.Display.V1.FormattedValue"


def _sessao(sid: str, status: int, fim: str, erro: str | None = "WindowsIdentityIncorrect") -> dict:
    return {
        "flowsessionid": sid, "statuscode": status, "completedon": fim, "errorcode": erro if status == 8 else None,
        "errormessage": "A tentativa de logon e invalida." if status == 8 else None,
        "_regardingobjectid_value": FLUXO, f"_regardingobjectid_value{ROTULO}": "RPA-CONTAS-A-RECEBER",
        f"_machineid_value{ROTULO}": "NOTEAUTSAP", f"runmode{ROTULO}": "Assistido(a)",
        "owninguser": {"fullname": "Hugo Reis", "internalemailaddress": "hugo@jfcagro.com.br"},
    }


def _coletor(repo: InMemoryRepository) -> DesktopFlowCollector:
    return DesktopFlowCollector(DataverseClient(DV, tenant_id="tenant", client_id="app", client_secret="s"), repo)


async def _rodar(repo: InMemoryRepository, sessoes: list[dict], mock: respx.MockRouter, coletor=None):  # noqa: ANN001, ANN202
    mock.post(TOKEN).respond(200, json={"access_token": "tk", "expires_in": 3600})
    rota = mock.get(SESSOES).respond(200, json={"value": sessoes})
    coletor = coletor or _coletor(repo)
    resultado = await coletor.run()
    return resultado, coletor, rota


class TestColeta:
    async def test_falhas_seguidas_somam_no_streak_do_fluxo(self) -> None:
        repo = InMemoryRepository()
        with respx.mock() as mock:
            sessoes = [_sessao(f"s{i}", 8, f"2026-10-14T1{i}:00:00Z") for i in range(3)]
            resultado, coletor, rota = await _rodar(repo, sessoes, mock)
            await coletor.client.aclose()
            filtro = rota.calls.last.request.url.params["$filter"]

        assert filtro.startswith("completedon ge "), "primeira coleta olha as ultimas 24 h"
        streak = resultado.extra["streaks"][f"pa:{FLUXO}"]
        assert streak.count == 3 and streak.workflow_name == "RPA-CONTAS-A-RECEBER"

    async def test_sucesso_encerra_o_incidente(self) -> None:
        repo = InMemoryRepository()
        with respx.mock() as mock:
            sessoes = [_sessao("s1", 8, "2026-10-14T10:00:00Z"), _sessao("s2", 4, "2026-10-14T11:00:00Z")]
            resultado, coletor, _ = await _rodar(repo, sessoes, mock)
            await coletor.client.aclose()
        assert resultado.extra["streaks"] == {} and repo.get_streak(f"pa:{FLUXO}") is None
        assert coletor.estado[f"pa:{FLUXO}"][0] == 4

    async def test_sessao_do_mesmo_segundo_nao_e_contada_duas_vezes(self) -> None:
        """`ge` no filtro + ids do ultimo segundo no cursor: nem perde nem duplica."""
        repo = InMemoryRepository()
        primeira = _sessao("s1", 8, "2026-10-14T10:00:00Z")
        with respx.mock() as mock:
            _, coletor, _ = await _rodar(repo, [primeira], mock)
            resultado, _, rota = await _rodar(repo, [primeira, _sessao("s2", 8, "2026-10-14T10:00:00Z")], mock, coletor)
            await coletor.client.aclose()

        assert rota.calls.last.request.url.params["$filter"] == "completedon ge 2026-10-14T10:00:00Z"
        assert resultado.collected == 1 and repo.get_streak(f"pa:{FLUXO}").count == 2
        cursor = json.loads(repo.get_cursor("powerautomate", "flowsessions").cursor)
        assert cursor["ids"] == ["s1", "s2"]

    async def test_dataverse_fora_vira_erro_e_mantem_o_cursor(self) -> None:
        repo = InMemoryRepository()
        with respx.mock() as mock:
            mock.post(TOKEN).mock(side_effect=httpx.ConnectError("sem rota"))
            coletor = _coletor(repo)
            resultado = await coletor.run()
            await coletor.client.aclose()
        assert not resultado.ok
        assert repo.get_cursor("powerautomate", "flowsessions").consecutive_failures == 1


class TestSessao:
    def test_sessao_ainda_rodando_e_ignorada(self) -> None:
        assert parse_sessao({**_sessao("s1", 2, "x"), "completedon": None}) is None

    def test_dono_sai_no_formato_que_o_resolver_entende(self) -> None:
        assert parse_sessao(_sessao("s1", 8, "2026-10-14T10:00:00Z")).dono == "Hugo Reis <hugo@jfcagro.com.br>"


def _evento(contagem: int = 3):  # noqa: ANN202
    sessao = parse_sessao(_sessao("s9", 8, "2026-10-14T12:00:00Z"))
    repo = InMemoryRepository()
    for i in range(contagem):
        streak = repo.bump_streak(sessao.chave, sessao.concluida_em - timedelta(hours=contagem - 1 - i), None, sessao.fluxo_nome)
    decisao = RuleEngine(load().policy).evaluate(event_type=TIPO, streak_count=streak.count, now=datetime.now(UTC))
    return evento_de_falha_desktop(streak, sessao, decisao)


class TestRegraEEvento:
    def test_tres_falhas_e_critico_uma_e_alto(self) -> None:
        assert _evento(3).severity == "critical" and _evento(3).detail["template"] == "desktop_flow_failing"
        assert _evento(1).severity == "high"

    def test_evento_nao_aponta_para_o_n8n(self) -> None:
        """`workflow_id` no detalhe faria o botao do e-mail abrir o n8n."""
        assert "workflow_id" not in _evento().detail


class TestReconferencia:
    async def test_sucesso_depois_da_falha_descarta_o_alerta(self) -> None:
        evento = _evento()
        coletor = _coletor(InMemoryRepository())
        coletor.estado[evento.resource_id] = (4, datetime(2026, 10, 14, 13, 0, tzinfo=UTC))
        checador = ResolutionChecker(containers=None, resources=None, n8n=None, repo=None,
                                     limites=load().policy.thresholds, desktop=coletor)
        assert await checador.motivo(evento) == "execucao posterior do RPA com sucesso"
        coletor.estado[evento.resource_id] = (8, datetime(2026, 10, 14, 13, 0, tzinfo=UTC))
        assert await checador.motivo(evento) is None
        await coletor.client.aclose()


class TestEmail:
    async def test_alerta_de_rpa_sai_no_padrao_para_os_destinatarios_da_fonte(self) -> None:
        import re
        from html import unescape

        from agent_nio_monitor.agent import Agent
        from agent_nio_monitor.notify.mailer import DispatchResult

        class _Captura:
            async def send(self, **kwargs: object) -> DispatchResult:
                self.enviado = kwargs
                return DispatchResult(status="sent", channel="teste")

        class _SemLlm:
            async def analyze(self, contexto: dict) -> tuple[None, str]:
                return None, "fallback"

        agente = Agent(load(), InMemoryRepository())
        agente.analyst, agente.notifier = _SemLlm(), _Captura()
        try:
            await agente.dispatch(_evento())
        finally:
            await agente.llm.aclose()
            await agente.n8n.aclose()
            if agente.desktop is not None:
                await agente.desktop.client.aclose()

        enviado = agente.notifier.enviado
        linhas = [x.strip() for x in unescape(re.sub(r"<[^>]+>", "\n", enviado["html"])).splitlines() if x.strip()]
        assert enviado["subject"].startswith("[CRITICO] RPA RPA-CONTAS-A-RECEBER falhou 3x")
        # `fontes: powerautomate` do people.yaml (rafael + carlos) + elton, que
        # entra em copia em TODO disparo por `sempre_incluir` (fixture do dono).
        assert sorted(enviado["recipients"]) == [
            "carlos.oliveira@grupocampanha.com.br", "elton.canto@grupocampanha.com.br",
            "rafael.barboza@grupocampanha.com.br",
        ], "alerta de RPA vai para `fontes: powerautomate` + copia permanente do elton"
        assert linhas[linhas.index("Responsável pelo fluxo") + 1] == "Hugo Reis <hugo@jfcagro.com.br>"
        assert "NOTEAUTSAP" in linhas[linhas.index("Detalhe") + 1]
        assert "Abrir no n8n" not in linhas
