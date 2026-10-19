"""Logs dos containers (ADR D40): erro novo, pico, ruido ignorado e falha de gatilho do n8n.

As linhas abaixo sao as que a sonda de 19/10/2026 encontrou na instancia (redigidas).
"""

from __future__ import annotations

from agent_nio_monitor.config.schema import LogsConfig
from agent_nio_monitor.sources.portainer.log_lines import assinatura, eh_erro, falha_de_trigger, separar
from agent_nio_monitor.sources.portainer.logs import ContainerLogCollector
from agent_nio_monitor.store.memory_repository import InMemoryRepository

TRIGGER_IMAP = ('The trigger node "Email Trigger (IMAP)1" of workflow "MB Email IMAP" failed with the error: '
                '"Imap connection closed unexpectedly"')


class TestLinha:
    def test_nivel_declarado_manda_sobre_a_palavra(self) -> None:
        assert not eh_erro("[2026-10-12 07:01:14] WARNING  hfbrasil | Tentativa 1/3 falhou para TOMATE: Timeout")
        assert not eh_erro("2026-10-16 19:11:12 | WARNING  | API falhou (tentativa 3/3): 429 Client Error")

    def test_erros_reais_sao_erro(self) -> None:
        assert eh_erro("      Erro registrado. Origem: DOCUMENTO_NAO_ENCONTRADO | Tentativas: 0")
        assert eh_erro("[Error: socket hang up] { code: 'ECONNRESET' }")
        assert eh_erro('{"error": "Azure AD recusou o token (400): invalid_request"}')

    def test_json_com_nivel_info_nao_e_erro(self) -> None:
        assert not eh_erro('{"level": "info", "msg": "error budget ok"}')

    def test_linha_comum_nao_e_erro(self) -> None:
        assert not eh_erro("GET /healthz 200 3ms")

    def test_mesma_mensagem_com_numero_e_id_diferentes_tem_a_mesma_assinatura(self) -> None:
        a = "Erro registrado. Origem: DOCUMENTO_NAO_ENCONTRADO | Tentativas: 0 | id 9f1c2a3b-1111-2222-3333-444455556666"
        b = "Erro registrado. Origem: DOCUMENTO_NAO_ENCONTRADO | Tentativas: 3 | id 0a0b0c0d-1111-2222-3333-444455556666"
        assert assinatura(a) == assinatura(b)
        assert assinatura(a) != assinatura("Erro registrado. Origem: TIMEOUT_SEFAZ")

    def test_separa_o_instante_do_docker(self) -> None:
        instante, texto = separar("2026-10-19T16:13:56.349300015Z ERRO: defina GRSA_EMAIL")
        assert instante.isoformat() == "2026-10-19T16:13:56.349300+00:00"
        assert texto == "ERRO: defina GRSA_EMAIL"

    def test_falha_de_gatilho_do_n8n(self) -> None:
        assert falha_de_trigger(TRIGGER_IMAP) == (
            "Email Trigger (IMAP)1", "MB Email IMAP", "Imap connection closed unexpectedly")


class PortainerFalso:
    def __init__(self) -> None:
        self.logs: dict[str, list[str]] = {}
        self.pedidos: list[dict] = []

    async def list_endpoints(self) -> list[dict]:
        return [{"Id": 3}]

    async def list_containers(self, _endpoint: int) -> list[dict]:
        return [{"Id": nome, "Names": [f"/{nome}"], "State": "running", "Labels": {}} for nome in self.logs]

    async def list_stacks(self) -> list[dict]:
        return []

    async def container_logs(self, _endpoint, container_id, **kw) -> str:  # noqa: ANN001, ANN003
        self.pedidos.append({"container": container_id, **kw})
        return "\n".join(self.logs[container_id])


def _linha(segundo: int, texto: str) -> str:
    return f"2026-10-19T12:00:{segundo:02d}.000000000Z {texto}"


async def _semeado(cfg: LogsConfig | None = None, **logs: list[str]) -> tuple[PortainerFalso, ContainerLogCollector]:
    portainer = PortainerFalso()
    portainer.logs = {nome: linhas for nome, linhas in logs.items()}
    coletor = ContainerLogCollector(portainer, InMemoryRepository(), cfg or LogsConfig())
    assert (await coletor.check_logs()).extra["eventos"] == [], "a semente nao alerta"
    return portainer, coletor


class TestColetor:
    async def test_semente_le_o_historico_e_depois_le_so_o_novo(self) -> None:
        portainer, coletor = await _semeado(api=[_linha(1, "ERRO: defina GRSA_EMAIL")])
        await coletor.check_logs()
        assert "since" not in portainer.pedidos[0] or portainer.pedidos[0]["since"] is None
        assert portainer.pedidos[1]["since"] == int(separar(_linha(1, "x"))[0].timestamp())

    async def test_mensagem_conhecida_no_ritmo_normal_nao_alerta(self) -> None:
        portainer, coletor = await _semeado(api=[_linha(1, "Erro registrado. Origem: X | Tentativas: 0")])
        portainer.logs["api"] = [_linha(2, "Erro registrado. Origem: X | Tentativas: 1")]
        assert (await coletor.check_logs()).extra["eventos"] == []

    async def test_mensagem_nova_alerta_com_chave_do_container(self) -> None:
        portainer, coletor = await _semeado(api=[_linha(1, "Erro registrado. Origem: X")])
        portainer.logs["api"] = [_linha(2, "Traceback: KeyError 'cliente'"), _linha(3, "Traceback: KeyError 'cliente'")]
        eventos = (await coletor.check_logs()).extra["eventos"]
        assert [e.event_type for e in eventos] == ["container_log_error"]
        assert eventos[0].detail["ocorrencias"] == 2 and "erro novo" in eventos[0].title
        assert eventos[0].resource_id == "api"

    async def test_um_traceback_de_varias_linhas_vira_um_alerta_so(self) -> None:
        """Caso real de 19/10/2026: um ForeignKeyViolation no integracao-dfe-xml virou 13 e-mails."""
        portainer, coletor = await _semeado(dfe=[_linha(1, "INFO sincronizacao iniciada")])
        portainer.logs["dfe"] = [_linha(17, texto) for texto in (
            "[ERROR] services.sap.sap_db - INV1 -> Erro SQLAlchemy: (psycopg2.errors.ForeignKeyViolation)",
            "Traceback (most recent call last):",
            'psycopg2.errors.ForeignKeyViolation: ERRO: viola restricao de chave estrangeira "FK_OINV"',
            "The above exception was the direct cause of the following exception:",
            "sqlalchemy.exc.IntegrityError: (psycopg2.errors.ForeignKeyViolation) ERRO: insercao em INV1",
            "[ERROR] routers.cargas_sap_router - Sincronizacao interrompida devido a falha em dependencia.",
        )]
        eventos = (await coletor.check_logs()).extra["eventos"]
        assert len(eventos) == 1 and eventos[0].resource_id == "dfe"
        assert len(eventos[0].detail["assinaturas"]) == 6, "6 linhas de erro distintas, UM alerta"
        assert "ForeignKeyViolation" in eventos[0].detail["detalhe"]

    async def test_linha_ja_lida_no_mesmo_segundo_nao_conta_de_novo(self) -> None:
        portainer, coletor = await _semeado(api=[_linha(5, "INFO pronto")])
        portainer.logs["api"] = [_linha(5, "Traceback: repetida"), _linha(6, "INFO ok")]
        assert (await coletor.check_logs()).extra["eventos"] == []

    async def test_pico_de_mensagem_conhecida_alerta(self) -> None:
        portainer, coletor = await _semeado(api=[_linha(1, "Erro registrado. Origem: X")])
        portainer.logs["api"] = [_linha(10 + i, "Erro registrado. Origem: X") for i in range(25)]
        eventos = (await coletor.check_logs()).extra["eventos"]
        assert len(eventos) == 1 and "pico de 25" in eventos[0].title

    async def test_regex_de_ignorar_do_policy(self) -> None:
        cfg = LogsConfig(ignorar={"nextjs_app": ["socket hang up"]})
        portainer, coletor = await _semeado(cfg, nextjs_app=[_linha(1, "INFO ok")])
        portainer.logs["nextjs_app"] = [_linha(2, "[Error: socket hang up] { code: 'ECONNRESET' }")]
        assert (await coletor.check_logs()).extra["eventos"] == []

    async def test_falha_de_gatilho_vira_evento_do_workflow_com_a_contagem_da_hora(self) -> None:
        portainer, coletor = await _semeado(n8n=[_linha(1, "Editor is now accessible")])
        portainer.logs["n8n"] = [_linha(10 + i, TRIGGER_IMAP) for i in range(3)]
        eventos = (await coletor.check_logs({"MB Email IMAP": "wf123"})).extra["eventos"]
        assert [e.event_type for e in eventos] == ["workflow_trigger_failed"]
        assert eventos[0].resource_id == "wf123" and eventos[0].streak_count == 3
        assert eventos[0].detail["no"] == "Email Trigger (IMAP)1"

    async def test_rodizio_le_todos_os_containers_em_ciclos(self) -> None:
        nomes = {f"c{i:02d}": [_linha(1, "INFO ok")] for i in range(5)}
        portainer, coletor = await _semeado(LogsConfig(max_containers_por_ciclo=2), **nomes)
        for _ in range(2):
            await coletor.check_logs()
        assert {p["container"] for p in portainer.pedidos} == set(nomes)
