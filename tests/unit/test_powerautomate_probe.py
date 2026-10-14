"""Sonda do Power Automate: cada falha aponta para o passo certo do portal.

Entra ID e Dataverse simulados com `respx`. O que importa e o diagnostico: um 401 do
Dataverse (falta usuario de aplicativo) nao pode ser confundido com o token recusado
pelo Entra (secret vencido), porque cada um se resolve num lugar diferente.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from pydantic import ValidationError

from agent_nio_monitor.config.schema import PowerAutomateConfig
from agent_nio_monitor.sources.powerautomate.client import DataverseClient
from agent_nio_monitor.sources.powerautomate.probe import atraso_de_ingestao, sondar, sondar_do_config

DV = "https://orgteste.crm2.dynamics.com"
API = f"{DV}/api/data/v9.2"
TOKEN = "https://login.microsoftonline.com/tenant/oauth2/v2.0/token"

EXECUCOES = [
    {"name": "r1", "status": "Failed", "endtime": "2026-10-14T12:00:00Z", "createdon": "2026-10-14T12:00:20Z"},
    {"name": "r2", "status": "Succeeded", "endtime": "2026-10-14T11:00:00Z", "createdon": "2026-10-14T11:00:40Z"},
    {"name": "r3", "status": "Succeeded", "endtime": None, "createdon": "2026-10-14T10:00:00Z"},
]

SESSOES = [
    {"statuscode": 8, "errorcode": "NoCandidateMachine", "completedon": "2026-10-14T12:00:00Z",
     "modifiedon": "2026-10-14T12:00:05Z"},
    {"statuscode": 4, "completedon": "2026-10-14T11:00:00Z", "modifiedon": "2026-10-14T11:00:03Z"},
    {"statuscode": 4, "completedon": None, "modifiedon": "2026-10-14T10:00:00Z"},
]


def _cliente() -> DataverseClient:
    return DataverseClient(DV, tenant_id="tenant", client_id="app", client_secret="s3gr3d0")


def _tudo_ok(mock: respx.MockRouter, *, ttl: int = 2419200, eventos: list | None = None) -> None:
    mock.post(TOKEN).respond(200, json={"access_token": "tk", "expires_in": 3600})
    mock.get(f"{API}/WhoAmI").respond(200, json={"UserId": "u-sp"})
    mock.get(f"{API}/organizations").respond(200, json={"value": [{"flowruntimetoliveinseconds": ttl}]})
    mock.get(f"{API}/workflows").respond(200, json={"value": [{"statecode": 1}, {"statecode": 0}]})
    mock.get(f"{API}/flowruns").respond(200, json={"value": EXECUCOES})
    mock.get(f"{API}/flowsessions").respond(200, json={"value": SESSOES})
    mock.get(f"{API}/flowevents").respond(200, json={"value": eventos or []})


def _por_nome(relatorio) -> dict:  # noqa: ANN001
    return {v.nome: v for v in relatorio.verificacoes}


class TestCaminhoFeliz:
    async def test_tudo_ok_mede_status_e_atraso(self) -> None:
        with respx.mock() as mock:
            _tudo_ok(mock)
            dv = _cliente()
            rel = _por_nome(await sondar(dv))
            await dv.aclose()

        assert all(v.ok for v in rel.values()), {n: v.detalhe for n, v in rel.items() if not v.ok}
        assert "Failed, Succeeded" in rel["execucoes"].detalhe, "os valores REAIS de status aparecem"
        assert "mediana 30s, pior 40s" in rel["atraso de ingestao"].detalhe
        assert "2 fluxo(s), 1 ligado(s)" in rel["fluxos de nuvem"].detalhe
        assert "Succeeded(4): 2, Failed(8): 1" in rel["execucoes de desktop"].detalhe
        assert "NoCandidateMachine" in rel["erros de desktop"].detalhe
        assert "mediana 4s, pior 5s" in rel["atraso desktop"].detalhe
        assert "28 dia(s)" in rel["retencao"].detalhe

    async def test_token_e_reaproveitado_entre_consultas(self) -> None:
        with respx.mock() as mock:
            _tudo_ok(mock)
            dv = _cliente()
            await sondar(dv)
            await dv.aclose()
            assert mock.routes[0].call_count == 1, "um token por sonda, nao um por consulta"


class TestDiagnostico:
    async def test_401_do_dataverse_aponta_o_usuario_de_aplicativo(self) -> None:
        with respx.mock() as mock:
            mock.post(TOKEN).respond(200, json={"access_token": "tk", "expires_in": 3600})
            mock.get(f"{API}/WhoAmI").respond(401, json={"error": {"message": "not a member"}})
            dv = _cliente()
            rel = await sondar(dv)
            await dv.aclose()

        assert len(rel.verificacoes) == 1, "sem acesso, o resto daria o mesmo erro"
        assert "Passo C" in rel.verificacoes[0].detalhe

    async def test_secret_vencido_nao_e_confundido_com_usuario_ausente(self) -> None:
        with respx.mock() as mock:
            mock.post(TOKEN).respond(401, json={"error_description": "AADSTS7000222: expired"})
            dv = _cliente()
            rel = await sondar(dv)
            await dv.aclose()

        detalhe = rel.verificacoes[0].detalhe
        assert "EXPIRADO" in detalhe and "Passo C" not in detalhe

    async def test_403_numa_tabela_aponta_a_funcao_e_nao_para_as_outras(self) -> None:
        with respx.mock() as mock:
            _tudo_ok(mock)
            mock.get(f"{API}/flowruns").respond(403, json={"error": {"message": "prvReadflowrun"}})
            dv = _cliente()
            rel = _por_nome(await sondar(dv))
            await dv.aclose()

        assert not rel["execucoes"].ok and "Passo B" in rel["execucoes"].detalhe
        assert rel["perdas de ingestao"].ok, "uma tabela sem permissao nao derruba as outras"

    async def test_403_real_diz_qual_privilegio_falta(self) -> None:
        """Formato real do 403 neste tenant: ~500 caracteres de ids e o privilegio no FIM."""
        ids = "Principal user (Id=e8ab, type=8, roleCount=1, privilegeCount=9, " + "x" * 400 + ")"
        with respx.mock() as mock:
            _tudo_ok(mock)
            mock.get(f"{API}/flowruns").respond(403, json={"error": {"message": f"{ids} is missing prvReadflowrun privilege"}})
            dv = _cliente()
            rel = _por_nome(await sondar(dv))
            await dv.aclose()
        assert "falta o privilegio prvReadflowrun" in rel["execucoes"].detalhe

    async def test_retencao_desligada_e_falha_explicita(self) -> None:
        with respx.mock() as mock:
            _tudo_ok(mock, ttl=0)
            dv = _cliente()
            rel = _por_nome(await sondar(dv))
            await dv.aclose()
        assert not rel["retencao"].ok and "Passo D" in rel["retencao"].detalhe

    async def test_aviso_de_perda_do_dataverse_aparece(self) -> None:
        with respx.mock() as mock:
            _tudo_ok(mock, eventos=[{"eventcode": "IngestionRateDataLoss"}])
            dv = _cliente()
            rel = _por_nome(await sondar(dv))
            await dv.aclose()
        assert "IngestionRateDataLoss" in rel["perdas de ingestao"].detalhe

    async def test_so_eventos_de_ingestao_contam_como_perda(self) -> None:
        """Visto na instancia: `AttendedNoLicense` (tipo Licensing, de desktop flow) nao e perda."""
        with respx.mock() as mock:
            _tudo_ok(mock)
            dv = _cliente()
            await sondar(dv)
            await dv.aclose()
            filtro = mock.routes[-1].calls.last.request.url.params["$filter"]
        assert filtro == "eventtype eq 'FlowRunIngestion'"

    async def test_rede_fora_vira_linha_e_nao_excecao(self) -> None:
        with respx.mock() as mock:
            mock.post(TOKEN).mock(side_effect=httpx.ConnectError("sem rota"))
            dv = _cliente()
            rel = await sondar(dv)
            await dv.aclose()
        assert "rede" in rel.verificacoes[0].detalhe


class TestDoConfig:
    async def test_credencial_ausente_lista_as_variaveis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for nome in ("POWERAUTOMATE_TENANT_ID", "POWERAUTOMATE_CLIENT_ID", "POWERAUTOMATE_CLIENT_SECRET"):
            monkeypatch.delenv(nome, raising=False)
        saida = await sondar_do_config(PowerAutomateConfig(dataverse_url=DV))
        assert "POWERAUTOMATE_CLIENT_SECRET" in saida

    def test_url_sem_https_e_recusada_no_boot(self) -> None:
        with pytest.raises(ValidationError):
            PowerAutomateConfig(dataverse_url="http://orgteste.crm2.dynamics.com")


def test_atraso_ignora_execucao_sem_fim() -> None:
    assert atraso_de_ingestao(EXECUCOES) == [20.0, 40.0]
