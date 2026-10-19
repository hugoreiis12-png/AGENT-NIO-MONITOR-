"""Aprofunda as sessoes novas de RPA: fase em que parou, acao que falhou e erro tratado em sessao verde.

O log de acoes sobe DEPOIS que a sessao termina (`logDetails.uploadStatus`). Sessao que ainda
nao tem log fica pendente e e reconsultada nos ciclos seguintes. Sessao que nao chegou a
executar (ex.: `NoCandidateMachine`, a maioria das falhas medidas) nunca tera log -- nao fica
pendente.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

import httpx
import structlog

from .client import DataverseClient, DataverseError
from .desktop import STATUS_FALHA, STATUS_SUCESSO, SessaoDesktop
from .session_detail import AnaliseAcoes, analisar_acoes, explicar_codigo, fase_com_falha, log_disponivel

log = structlog.get_logger(__name__)

MAX_LOGS_POR_CICLO = 10
MAX_TENTATIVAS = 10


@dataclass
class Aprofundamento:
    detalhes: dict[str, dict[str, str]] = field(default_factory=dict)
    tratadas: dict[str, tuple[SessaoDesktop, AnaliseAcoes]] = field(default_factory=dict)


def executou(rundetails: str | None) -> bool:
    try:
        return "execution" in ((json.loads(rundetails or "{}") or {}).get("runtimeSteps") or {})
    except ValueError:
        return False


class LeitorDeAcoes:
    def __init__(self, client: DataverseClient) -> None:
        self.client = client
        self._pendentes: dict[str, tuple[SessaoDesktop, int]] = {}
        # Chave do fluxo -> (fim da ultima sessao verde lida, se ela teve erro tratado): a fila reconfere aqui.
        self.ultimo_tratado: dict[str, tuple[datetime, bool]] = {}
        # Inicio do incidente de erro tratado por fluxo: fecha na primeira sessao verde limpa.
        self.incidente_tratado: dict[str, datetime] = {}

    async def aprofundar(self, sessoes: list[SessaoDesktop]) -> Aprofundamento:
        resultado = Aprofundamento()
        for s in sessoes:
            if s.status in STATUS_FALHA:
                resultado.detalhes[s.id] = _sem_log(s)
            if executou(s.rundetails):
                self._pendentes[s.id] = (s, 0)
        lidos = 0
        for sid, (sessao, tentativas) in list(self._pendentes.items()):
            if lidos >= MAX_LOGS_POR_CICLO:
                break
            try:
                lidos += await self._tentar(sid, sessao, tentativas, resultado)
            except (DataverseError, httpx.HTTPError) as exc:
                log.warning("log_de_acoes_ilegivel", sessao=sid, erro=str(exc))
                self._adiar(sid, sessao, tentativas)
        return resultado

    async def _tentar(self, sid: str, sessao: SessaoDesktop, tentativas: int, resultado: Aprofundamento) -> int:
        rundetails = sessao.rundetails if tentativas == 0 else await self._rundetails(sid)
        if not log_disponivel(rundetails):
            self._adiar(sid, sessao, tentativas)
            return 0
        self._pendentes.pop(sid, None)
        self._aplicar(sessao, analisar_acoes(await self.client.arquivo(f"flowsessions({sid})", "additionalcontext")),
                      resultado)
        return 1

    async def _rundetails(self, sid: str) -> str | None:
        return (await self.client.get(f"flowsessions({sid})", {"$select": "rundetails"})).get("rundetails")

    def _adiar(self, sid: str, sessao: SessaoDesktop, tentativas: int) -> None:
        if tentativas + 1 >= MAX_TENTATIVAS:
            self._pendentes.pop(sid, None)
            log.warning("log_de_acoes_nao_subiu", sessao=sid, fluxo=sessao.fluxo_nome)
            return
        self._pendentes[sid] = (sessao, tentativas + 1)

    def _aplicar(self, sessao: SessaoDesktop, analise: AnaliseAcoes, resultado: Aprofundamento) -> None:
        if sessao.status in STATUS_FALHA and analise.falha:
            resultado.detalhes.setdefault(sessao.id, {})["acao"] = analise.falha.descricao()
        if sessao.status != STATUS_SUCESSO:
            return
        self.ultimo_tratado[sessao.chave] = (sessao.concluida_em, bool(analise.tratadas))
        if not analise.tratadas:
            self.incidente_tratado.pop(sessao.chave, None)
            return
        self.incidente_tratado.setdefault(sessao.chave, sessao.concluida_em)
        resultado.tratadas[sessao.chave] = (sessao, analise)


def _sem_log(sessao: SessaoDesktop) -> dict[str, str]:
    """O que se sabe sem o log de acoes: a fase e o significado do codigo."""
    fase = fase_com_falha(sessao.rundetails)
    if fase is None:
        fase = "durante a execucao" if executou(sessao.rundetails) else "antes de executar (nenhuma acao rodou)"
    detalhes = {"fase": fase}
    if explicacao := explicar_codigo(sessao.erro_codigo):
        detalhes["explicacao"] = explicacao
    return detalhes
