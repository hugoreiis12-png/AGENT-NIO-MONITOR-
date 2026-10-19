"""RPA por fase e por acao (ADR D41).

`rundetails` e o log de acoes (`additionalcontext`) seguem o formato lido no ambiente
RPA-CONTAS-A-RECEBER em 19/10/2026. O campo de erro de uma acao com falha NAO foi visto
em dado real (nenhuma acao fora de `Succeeded` nas 60 sessoes lidas): os testes travam o
comportamento defensivo, nao um formato confirmado.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from agent_nio_monitor.sources.powerautomate.action_logs import LeitorDeAcoes
from agent_nio_monitor.sources.powerautomate.desktop import SessaoDesktop
from agent_nio_monitor.sources.powerautomate.events import evento_erros_tratados
from agent_nio_monitor.sources.powerautomate.session_detail import (
    analisar_acoes,
    explicar_codigo,
    fase_com_falha,
    log_disponivel,
)

RUN_OK = json.dumps({
    "runtimeSteps": {"start": {"state": "Success"}, "assignment": {"state": "Success"},
                     "execution": {"duration": 67416, "state": "Success"}},
    "logDetails": {"uploadStatus": "Completed", "version": "AdditionalContext"},
})
RUN_SEM_MAQUINA = json.dumps({"logDetails": {"uploadStatus": "NotStarted", "version": "AdditionalContext"}})
RUN_LOG_SUBINDO = json.dumps({
    "runtimeSteps": {"execution": {"state": "Success"}}, "logDetails": {"uploadStatus": "InProgress"},
})


def _acao(nome: str, indice: int, status: str = "Succeeded", tratada: bool = False, **extra) -> dict:
    return {"name": nome, "functionName": "main", "index": indice, "status": status,
            "insideErrorHandling": tratada, **extra}


def _log(*acoes: dict) -> bytes:
    return json.dumps({"actions": list(acoes), "childFlowsLogStatus": "Ok"}).encode()


def _sessao(sid: str, status: int, rundetails: str, codigo: str | None = None) -> SessaoDesktop:
    return SessaoDesktop(
        id=sid, fluxo_id="f-1", fluxo_nome="CONTAS-A-RECEBER", status=status,
        concluida_em=datetime(2026, 10, 19, 12, tzinfo=UTC), erro_codigo=codigo, erro_mensagem=None,
        maquina="NOTEAUTSAP", modo="Assistido(a)", dono=None, rundetails=rundetails,
    )


class TestSessao:
    def test_fase_que_falhou(self) -> None:
        detalhes = json.dumps({"runtimeSteps": {"queue": {"state": "Success"}, "assignment": {"state": "Failed"}}})
        assert fase_com_falha(detalhes) == "assignment: Failed"
        assert fase_com_falha(RUN_OK) is None

    def test_log_so_disponivel_depois_do_upload(self) -> None:
        assert log_disponivel(RUN_OK)
        assert not log_disponivel(RUN_LOG_SUBINDO) and not log_disponivel(RUN_SEM_MAQUINA)

    def test_codigos_medidos_tem_explicacao(self) -> None:
        assert "maquina" in explicar_codigo("NoCandidateMachine")
        assert explicar_codigo("CodigoQueNinguemViu") is None

    def test_acoes_tratadas_e_falha_final_sao_separadas(self) -> None:
        analise = analisar_acoes(_log(
            _acao("Abrir Excel", 1), _acao("Ler celula", 2, "Failed", tratada=True, error={"message": "vazia"}),
            _acao("Salvar", 3, "Failed", errorMessage="arquivo bloqueado"),
        ))
        assert analise.total == 3
        assert [a.nome for a in analise.tratadas] == ["Ler celula"] and analise.tratadas[0].erro == "vazia"
        assert analise.falha.nome == "Salvar" and "arquivo bloqueado" in analise.falha.descricao()

    def test_log_ilegivel_nao_derruba(self) -> None:
        assert analisar_acoes(b"nao e json").total == 0


class DataverseFalso:
    def __init__(self, arquivos: dict[str, bytes], rundetails: dict[str, str] | None = None) -> None:
        self.arquivos, self.rundetails, self.baixados = arquivos, rundetails or {}, []

    async def arquivo(self, recurso: str, coluna: str) -> bytes | None:
        sid = recurso.removeprefix("flowsessions(").removesuffix(")")
        self.baixados.append(sid)
        return self.arquivos.get(sid)

    async def get(self, recurso: str, _params: dict) -> dict:
        return {"rundetails": self.rundetails[recurso.removeprefix("flowsessions(").removesuffix(")")]}


class TestLeitor:
    async def test_falha_antes_de_executar_nao_espera_log(self) -> None:
        cliente = DataverseFalso({})
        fundo = await LeitorDeAcoes(cliente).aprofundar([_sessao("s1", 8, RUN_SEM_MAQUINA, "NoCandidateMachine")])
        assert fundo.detalhes["s1"]["fase"].startswith("antes de executar")
        assert "maquina" in fundo.detalhes["s1"]["explicacao"]
        assert cliente.baixados == []

    async def test_sessao_verde_com_erro_tratado(self) -> None:
        cliente = DataverseFalso({"s2": _log(_acao("A", 1), _acao("B", 2, "Failed", tratada=True))})
        leitor = LeitorDeAcoes(cliente)
        fundo = await leitor.aprofundar([_sessao("s2", 4, RUN_OK)])
        sessao, analise = fundo.tratadas["pa:f-1"]
        evento = evento_erros_tratados(sessao, analise)
        assert evento.event_type == "desktop_flow_handled_errors" and "1 de 2 acoes" in evento.detail["detalhe"]
        assert leitor.ultimo_tratado["pa:f-1"][1] is True

    async def test_falha_durante_a_execucao_aponta_a_acao(self) -> None:
        cliente = DataverseFalso({"s3": _log(_acao("A", 1), _acao("Salvar", 2, "Failed"))})
        fundo = await LeitorDeAcoes(cliente).aprofundar([_sessao("s3", 8, RUN_OK, "ActionFailed")])
        assert fundo.detalhes["s3"]["acao"].startswith("Salvar (subfluxo main, acao #2)")

    async def test_log_que_ainda_nao_subiu_e_lido_no_ciclo_seguinte(self) -> None:
        cliente = DataverseFalso({"s4": _log(_acao("A", 1, "Failed", tratada=True))}, {"s4": RUN_OK})
        leitor = LeitorDeAcoes(cliente)
        assert (await leitor.aprofundar([_sessao("s4", 4, RUN_LOG_SUBINDO)])).tratadas == {}
        assert "pa:f-1" in (await leitor.aprofundar([])).tratadas
