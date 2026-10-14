"""Coleta das execucoes de fluxo de desktop (RPA) na tabela `flowsession` do Dataverse.

Fluxo de desktop grava SEMPRE em `flowsession`, dentro ou fora de solucao. Medido no
ambiente RPA-CONTAS-A-RECEBER: 8 = Failed, 4 = Succeeded, e a sessao aparece no
Dataverse no mesmo segundo em que termina.

Cursor por `completedon` (precisao de segundo no Dataverse): a consulta usa `ge` e o
cursor guarda os ids ja vistos naquele segundo -- com `gt`, uma sessao que termina no
mesmo segundo da ultima lida, mas entra depois da consulta, sumiria em silencio.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from ...store.repository import CursorState, Repository, Streak, now_utc
from ..base import CollectorResult
from .client import DataverseClient, DataverseError

log = structlog.get_logger(__name__)

STATUS_SUCESSO = 4
STATUS_FALHA = frozenset({8, 9, 10})  # Failed, Faulted, TimedOut
JANELA_INICIAL = timedelta(hours=24)
MAX_SESSOES_POR_CICLO = 500
PREFIXO_DA_CHAVE = "pa:"
ROTULO = "@OData.Community.Display.V1.FormattedValue"
SELECT = ",".join((
    "flowsessionid", "statuscode", "startedon", "completedon", "errorcode", "errormessage",
    "runmode", "_regardingobjectid_value", "_machineid_value",
))


@dataclass(frozen=True)
class SessaoDesktop:
    id: str
    fluxo_id: str
    fluxo_nome: str
    status: int
    concluida_em: datetime
    erro_codigo: str | None
    erro_mensagem: str | None
    maquina: str | None
    modo: str | None
    dono: str | None

    @property
    def chave(self) -> str:
        """Chave do streak: prefixada para nao colidir com ids de workflow do n8n."""
        return f"{PREFIXO_DA_CHAVE}{self.fluxo_id}"


def _instante(texto: str) -> datetime:
    return datetime.fromisoformat(texto.replace("Z", "+00:00")).astimezone(UTC)


def _dono(raw: dict[str, Any]) -> str | None:
    """'Nome <email>': o resolver de destinatarios extrai o e-mail desse formato."""
    usuario = raw.get("owninguser") or {}
    nome, email = usuario.get("fullname"), usuario.get("internalemailaddress")
    if email:
        return f"{nome} <{email}>" if nome else email
    return nome


def parse_sessao(raw: dict[str, Any]) -> SessaoDesktop | None:
    """None quando falta o essencial (fluxo ou fim): sessao ainda rodando ou orfa."""
    fluxo_id, fim = raw.get("_regardingobjectid_value"), raw.get("completedon")
    if not fluxo_id or not fim:
        return None
    return SessaoDesktop(
        id=str(raw.get("flowsessionid")), fluxo_id=str(fluxo_id),
        fluxo_nome=raw.get(f"_regardingobjectid_value{ROTULO}") or str(fluxo_id),
        status=int(raw.get("statuscode") or 0), concluida_em=_instante(fim),
        erro_codigo=raw.get("errorcode"), erro_mensagem=raw.get("errormessage"),
        maquina=raw.get(f"_machineid_value{ROTULO}"), modo=raw.get(f"runmode{ROTULO}"), dono=_dono(raw),
    )


def _ler_cursor(texto: str | None) -> tuple[datetime | None, set[str]]:
    if not texto:
        return None, set()
    dados = json.loads(texto)
    return _instante(dados["ate"]), set(dados.get("ids", []))


def _novo_cursor(sessoes: list[SessaoDesktop], ate: datetime | None, ids: set[str]) -> str | None:
    if not sessoes:
        return json.dumps({"ate": ate.isoformat(), "ids": sorted(ids)}) if ate else None
    ultimo = sessoes[-1].concluida_em
    no_ultimo_segundo = {s.id for s in sessoes if s.concluida_em == ultimo}
    if ultimo == ate:
        no_ultimo_segundo |= ids
    return json.dumps({"ate": ultimo.isoformat(), "ids": sorted(no_ultimo_segundo)})


class DesktopFlowCollector:
    source = "powerautomate"
    scope = "flowsessions"

    def __init__(self, client: DataverseClient, repo: Repository) -> None:
        self.client, self.repo = client, repo
        # Ultimo (status, fim) por chave: a reconferencia da fila le daqui, sem nova consulta.
        self.estado: dict[str, tuple[int, datetime]] = {}

    async def run(self) -> CollectorResult:
        inicio = now_utc()
        state = self.repo.get_cursor(self.source, self.scope)
        ate, ids_vistos = _ler_cursor(state.cursor if state else None)
        desde = ate or inicio - JANELA_INICIAL
        try:
            brutas = await self._consultar(desde)
        except (DataverseError, httpx.HTTPError) as exc:
            return self._falhou(state, exc)
        sessoes = sorted(
            (s for s in map(parse_sessao, brutas) if s and s.id not in ids_vistos), key=lambda s: s.concluida_em,
        )
        streaks, falhas = self._acumular(sessoes)
        self.repo.set_cursor(CursorState(
            source=self.source, scope=self.scope, cursor=_novo_cursor(sessoes, ate, ids_vistos),
            last_success_at=now_utc(), consecutive_failures=0,
        ))
        return CollectorResult(
            collected=len(sessoes), failures=len(falhas),
            duration_ms=int((now_utc() - inicio).total_seconds() * 1000),
            extra={"streaks": streaks, "ultimas_falhas": falhas},
        )

    async def _consultar(self, desde: datetime) -> list[dict[str, Any]]:
        dados = await self.client.get("flowsessions", {
            "$select": SELECT,
            "$expand": "owninguser($select=fullname,internalemailaddress)",
            "$filter": f"completedon ge {desde.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": "completedon asc", "$top": str(MAX_SESSOES_POR_CICLO),
        }, anotacoes=True)
        return dados.get("value", [])

    def _acumular(self, sessoes: list[SessaoDesktop]) -> tuple[dict[str, Streak], dict[str, SessaoDesktop]]:
        """Falha soma no streak do fluxo; sucesso encerra o incidente (o proximo comeca do zero)."""
        streaks: dict[str, Streak] = {}
        falhas: dict[str, SessaoDesktop] = {}
        for s in sessoes:
            self.estado[s.chave] = (s.status, s.concluida_em)
            if s.status in STATUS_FALHA:
                streaks[s.chave] = self.repo.bump_streak(s.chave, s.concluida_em, None, s.fluxo_nome)
                falhas[s.chave] = s
            elif s.status == STATUS_SUCESSO:
                self.repo.resolve_streak(s.chave, s.concluida_em)
                streaks.pop(s.chave, None)
                falhas.pop(s.chave, None)
        return streaks, falhas

    def _falhou(self, state: CursorState | None, exc: Exception) -> CollectorResult:
        log.error("coleta_power_automate_falhou", erro=str(exc))
        self.repo.set_cursor(CursorState(
            source=self.source, scope=self.scope, cursor=state.cursor if state else None,
            last_success_at=None, consecutive_failures=(state.consecutive_failures if state else 0) + 1,
        ))
        return CollectorResult(error=str(exc))
