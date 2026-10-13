"""Portainer CE, feature-detect e health.

Header do Portainer e `X-API-Key`. O doc original usava `Authorization: Bearer`
-- o teste abaixo trava a razao.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
import respx

from agent_nio_monitor.capabilities import (
    detect,
    probe_n8n,
    render_table,
)
from agent_nio_monitor.health import HealthState
from agent_nio_monitor.sources.portainer.client import (
    PortainerClient,
    parse_container,
    resource_control_users,
    stack_mudou,
)

PTAI = "https://portainer.test"
N8N = "http://n8n.test"


class TestClientePortainer:
    @respx.mock
    async def test_header_e_x_api_key(self) -> None:
        rota = respx.get(f"{PTAI}/api/stacks").mock(
            return_value=httpx.Response(200, json=[])
        )
        async with PortainerClient(PTAI, "tok") as c:
            await c.list_stacks()
        assert rota.calls[0].request.headers["X-API-Key"] == "tok"
        assert "Authorization" not in rota.calls[0].request.headers

    @respx.mock
    async def test_containers_e_uma_chamada_em_lote(self) -> None:
        """Orcamento de rate limit do Portainer e 30 req/min. List+inspect por
        container estoura. A chamada em lote e o que mantem dentro."""
        rota = respx.get(
            f"{PTAI}/api/endpoints/1/docker/containers/json"
        ).mock(return_value=httpx.Response(200, json=[{"Id": "abc", "Names": ["/x"]}]))
        async with PortainerClient(PTAI, "tok") as c:
            containers = await c.list_containers(1)
        assert len(containers) == 1
        assert rota.call_count == 1
        assert rota.calls[0].request.url.params["all"] == "true"

    @respx.mock
    async def test_403_diz_que_precisa_admin(self) -> None:
        from agent_nio_monitor.sources.portainer.client import PortainerApiError

        respx.get(f"{PTAI}/api/users").mock(return_value=httpx.Response(403, json={}))
        async with PortainerClient(PTAI, "tok") as c:
            with pytest.raises(PortainerApiError, match="admin"):
                await c._get("/api/users")


class TestParseContainer:
    def test_health_e_extraido_do_status(self) -> None:
        assert parse_container({"Status": "Up 2 hours (healthy)"})["health"] == "healthy"
        assert parse_container({"Status": "Up 5 min (unhealthy)"})["health"] == "unhealthy"
        assert parse_container({"Status": "Exited (0)"})["health"] is None

    def test_restart_count_fica_none_e_nao_zero(self) -> None:
        """O endpoint de lista NAO traz RestartCount. Botar 0 seria mentira, e
        mentira aqui significa detector de restart-loop que nunca dispara."""
        p = parse_container({"Id": "abc", "Names": ["/worker"]})
        assert p["restart_count"] is None

    def test_nome_sem_barra(self) -> None:
        assert parse_container({"Names": ["/api-gateway"]})["name"] == "api-gateway"

    def test_container_inexistente_nao_quebra(self) -> None:
        p = parse_container({})
        assert p["name"] == "?"
        assert p["health"] is None


class TestResourceControl:
    def test_extrai_usuarios_do_stack(self) -> None:
        stack = {
            "Id": 1,
            "ResourceControl": {"UserAccessPolicies": {"3": {"RoleID": 1}, "7": {"RoleID": 2}}},
        }
        assert resource_control_users(stack) == ["3", "7"]

    def test_stack_sem_resource_control(self) -> None:
        assert resource_control_users({"Id": 1, "ResourceControl": None}) == []


class TestDeteccaoDeMudancaDeStack:
    @pytest.mark.parametrize(
        "campo", ["UpdateDate", "UpdatedBy", "ConfigHash", "Status", "ProjectPath"]
    )
    def test_campo_mudado_indica_mudanca(self, campo: str) -> None:
        antes = {"Id": 1, "UpdateDate": 1, "UpdatedBy": "a", "ConfigHash": "h",
                 "Status": 1, "ProjectPath": "/p"}
        agora = {**antes, campo: "diferente"}
        assert stack_mudou(antes, agora) is True

    def test_nada_mudado(self) -> None:
        s = {"Id": 1, "UpdateDate": 1, "UpdatedBy": "a"}
        assert stack_mudou(s, dict(s)) is False

    def test_primeira_vista_e_mudanca(self) -> None:
        assert stack_mudou(None, {"Id": 1}) is True


def _mock_n8n_base() -> None:
    """Rota coringa: tudo que o probe toca e nao mockado explicitamente devolve
    404. Sem isso o respx aborta a probe no primeiro endpoint nao mockado."""
    respx.route(method="GET", host="n8n.test").mock(
        return_value=httpx.Response(404, json={"message": "nao mockado"})
    )


class TestFeatureDetect:
    """O catch-all de host tem que ser registrado DEPOIS das rotas especificas.

    O respx casa na ordem de registro: quem registra primeiro vence. Com o
    catch-all na frente, toda rota especifica vira 404 e o probe mente.
    """

    @staticmethod
    def _catch_all(status: int = 404) -> None:
        respx.route(method="GET", host="n8n.test").mock(
            return_value=httpx.Response(status, json={"message": "nao mockado"})
        )

    @respx.mock
    async def test_detecta_versao_real_do_n8n(self) -> None:
        respx.get(f"{N8N}/healthz").mock(
            return_value=httpx.Response(200, json={"status": "ok", "version": "1.102.4"})
        )
        respx.get(f"{N8N}/api/v1/workflows").mock(return_value=httpx.Response(200, json={"data": []}))
        self._catch_all()
        report = await probe_n8n(N8N, "k")
        assert report.n8n_version == "1.102.4", (
            "a versao e a resposta do proprio servidor -- nunca supor"
        )

    @respx.mock
    async def test_rota_inexistente_marca_como_indisponivel(self) -> None:
        respx.get(f"{N8N}/healthz").mock(
            return_value=httpx.Response(200, json={"version": "1.102.4"})
        )
        respx.get(f"{N8N}/api/v1/workflows").mock(return_value=httpx.Response(200, json={}))
        self._catch_all()
        report = await probe_n8n(N8N, "k")
        assert report.has("n8n", "versioning") is False

    @respx.mock
    async def test_healthz_responde_mesmo_sem_permissao(self) -> None:
        """/healthz nao exige auth -- e por isso da para detectar n8n morto
        mesmo com a key revogada."""
        respx.get(f"{N8N}/healthz").mock(
            return_value=httpx.Response(200, json={"version": "1.102.4"})
        )
        respx.get(f"{N8N}/api/v1/workflows").mock(return_value=httpx.Response(403, json={}))
        self._catch_all()
        report = await probe_n8n(N8N, "chave-errada")
        assert report.has("n8n", "health") is True
        assert report.has("n8n", "core") is False

    @respx.mock
    async def test_n8n_fora_do_ar_nao_levanta(self) -> None:
        respx.route(method="GET", host="n8n.test").mock(
            side_effect=httpx.ConnectError("recusado")
        )
        report = await probe_n8n(N8N, "k")
        assert report.n8n_version is None
        assert all(not c.available for c in report.items if c.required)

    @respx.mock
    async def test_tabela_humana_mostra_a_versao(self) -> None:
        respx.get(f"{N8N}/healthz").mock(
            return_value=httpx.Response(200, json={"version": "9.9.9"})
        )
        respx.get(f"{N8N}/api/v1/workflows").mock(return_value=httpx.Response(200, json={}))
        self._catch_all()
        tabela = render_table(await probe_n8n(N8N, "k"))
        # O rotulo mudou de "VERSAO REAL DO N8N" para "VERSAO REPORTADA PELO N8N".
        # "REAL" era mentira: a versao e o que o servidor diz, e nesta instalacao
        # ele nao diz nada (verificado -- /healthz devolve so {"status":"ok"}).
        assert "VERSAO REPORTADA PELO N8N: 9.9.9" in tabela

    @respx.mock
    async def test_versao_ausente_e_declarada_como_nao_exposta(self) -> None:
        """Ausencia de versao tem que ser DECLARADA, nao omitida.

        Omitir faz parecer que o agente esqueceu de olhar; declarar registra
        que a decisao e por capacidades, que e medida.
        """
        self._catch_all()
        respx.get(f"{N8N}/healthz").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        respx.get(f"{N8N}/api/v1/workflows").mock(return_value=httpx.Response(200, json={}))
        tabela = render_table(await probe_n8n(N8N, "k"))
        assert "nao exposta por esta instancia" in tabela
        assert "CAPACIDADES" in tabela, "a saida tem de dizer que decide por capacidades"

    async def test_detect_ignora_fonte_sem_credencial(self) -> None:
        reports = await detect({})
        assert reports == {}


class TestHealth:
    def test_fonte_que_nao_coleta_ha_fica_stale(self) -> None:
        """'Esta rodando' e diferente de 'esta coletando'. O cron externo
        precisa ver a diferenca, senao um agente travado parece saudavel."""
        from datetime import UTC, datetime

        h = HealthState()
        h.mark("n8n_executions", True)
        assert h.stale_sources(timedelta(minutes=10)) == []

        # avanca o relogio em vez de confiar na resolucao do clock do Windows
        h.last_collect_at["n8n_executions"] = datetime.now(UTC) - timedelta(minutes=11)
        assert h.stale_sources(timedelta(minutes=10)) == ["n8n_executions"]
        # e volta a ser considered saudavel assim que coleta de novo
        h.mark("n8n_executions", True)
        assert h.stale_sources(timedelta(minutes=10)) == []

    def test_erro_consecutivo_e_contado(self) -> None:
        h = HealthState()
        h.mark("n8n", False)
        h.mark("n8n", False)
        assert h.collect_errors["n8n"] == 2
        h.mark("n8n", True)
        assert h.collect_errors["n8n"] == 0

    def test_snapshot_tem_o_que_o_cron_externo_precisa(self) -> None:
        h = HealthState()
        h.mark("n8n_executions", True)
        snap = h.snapshot(timedelta(minutes=10))
        for chave in ("status", "uptime_seconds", "heartbeat_at", "last_collect",
                      "stale_sources", "gaps_detected"):
            assert chave in snap

    def test_buraco_contado(self) -> None:
        h = HealthState()
        h.gaps_detected += 1
        assert h.snapshot(timedelta(minutes=10))["gaps_detected"] == 1


class TestNotificador:
    async def test_n8n_morto_vai_para_log_com_prefixo(self, capsys) -> None:
        """Decisao do dono do projeto. O prefixo fixo existe para um shipper
        conseguir separar alerta de log comum."""
        import json

        from agent_nio_monitor.notify.mailer import LOG_PREFIX, Notifier

        async with Notifier(webhook_url="http://n8n.test/webhook/x") as n:
            r = await n.send(
                recipients=["a@b.c"], subject="[CRITICO] n8n fora",
                body="corpo", n8n_up=False,
            )
        capturado = capsys.readouterr()
        assert r.status == "sent_to_log"
        assert LOG_PREFIX in capturado.err
        payload = json.loads(capturado.err.strip().split(" ", 1)[1])
        assert payload["para"] == ["a@b.c"]

    async def test_webhook_quebrado_cai_para_log(self, respx_mock) -> None:
        """Se o n8n esta fora, o proprio webhook falha. O alerta nao pode ser
        descartado por causa disso."""
        respx_mock.post("http://n8n.test/webhook/x").mock(
            return_value=httpx.Response(503)
        )
        from agent_nio_monitor.notify.mailer import Notifier

        async with Notifier(webhook_url="http://n8n.test/webhook/x", max_attempts=2) as n:
            r = await n.send(recipients=["a@b.c"], subject="s", body="b", n8n_up=True)
        assert r.status == "sent_to_log"
        assert r.attempts == 2

    async def test_sem_destinatario_falha_visivel(self) -> None:
        """'Ninguem notificado' precisa ser erro, nao sucesso silencioso."""
        from agent_nio_monitor.notify.mailer import Notifier

        async with Notifier(webhook_url="http://x/y") as n:
            r = await n.send(recipients=[], subject="s", body="b")
        assert r.status == "failed"
        assert "destinatario" in (r.error or "")


class TestEscalacao:
    def test_escalona_por_nao_reconhecido(self) -> None:
        from datetime import UTC, datetime

        from agent_nio_monitor.notify.mailer import Escalator

        esc = Escalator([15, 45])
        agora = datetime.now(UTC)
        assert esc.due(notified_at=None, now=agora) is None
        assert esc.due(notified_at=agora - timedelta(minutes=20), now=agora) == 15
        assert esc.due(notified_at=agora - timedelta(minutes=50), now=agora) == 45
        assert esc.due(notified_at=agora - timedelta(minutes=5), now=agora) is None

    def test_nao_escalona_o_mesmo_degrau_duas_vezes(self) -> None:
        from datetime import UTC, datetime

        from agent_nio_monitor.notify.mailer import Escalator

        esc = Escalator([15, 45])
        agora = datetime.now(UTC)
        enviado = agora - timedelta(minutes=50)
        assert esc.next_step(last_step=15, now=agora, notified_at=enviado) == 45
        assert esc.next_step(last_step=45, now=agora, notified_at=enviado) is None


class TestEscalonadorNaoEhRepeticao:
    def test_politica_nao_fica_com_resend_de_email(self) -> None:
        """O doc original tinha `action: resend`. Reenviar o mesmo email 3x nao e
        escalacao -- e treino para o time ignorar alerta."""
        import yaml
        from pathlib import Path

        politica = yaml.safe_load(
            (Path(__file__).resolve().parents[2] / "config" / "policy.yaml")
            .read_text(encoding="utf-8")
        )
        texto = str(politica)
        assert "resend" not in texto
        assert "escalate_after_minutes" in texto


def test_asyncio_disponivel() -> None:
    """Sanidade do fixture async deste arquivo."""
    assert asyncio.get_event_loop_policy() is not None
