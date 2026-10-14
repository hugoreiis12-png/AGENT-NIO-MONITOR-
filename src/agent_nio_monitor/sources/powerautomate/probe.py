"""Sonda do Power Automate (ADR D2): mede o que o Dataverse entrega antes de existir coletor.

Responde, com dado da instancia e nao da documentacao: o SP entra? le as execucoes?
quais valores reais tem `status`? quanto tempo a execucao demora para chegar ao
Dataverse? a retencao esta ligada? houve perda de ingestao?
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from ...config.schema import PowerAutomateConfig
from .client import DataverseClient, DataverseError
from .suite import cliente_do_config, credenciais_ausentes

AMOSTRA_DE_EXECUCOES = 50
AMOSTRA_DE_EVENTOS = 20
ESTADO_LIGADO = 1
CATEGORIA_NUVEM = 5
CATEGORIA_DESKTOP = 6
# `flowsession.statuscode` pela referencia da tabela; a sonda mostra o numero junto para conferir.
STATUS_DESKTOP = {
    4: "Succeeded", 5: "Skipped", 7: "Cancelled", 8: "Failed", 9: "Faulted",
    10: "TimedOut", 11: "Aborted", 2: "Running", 3: "Waiting", 1: "Paused",
}
TIPO_PERDA_DE_INGESTAO = "FlowRunIngestion"

# O que cada status HTTP quer dizer no Dataverse, apontando para o passo do portal.
DIAGNOSTICO_HTTP = {
    401: "SP nao reconhecido neste ambiente: falta o usuario de aplicativo (Passo C) ou a URL e de outro ambiente",
    403: "SP entrou, mas a funcao NIO Monitor Leitura nao da Read (Organization) nesta tabela (Passo B)",
    404: "tabela inexistente neste ambiente",
}


@dataclass
class Verificacao:
    nome: str
    ok: bool
    detalhe: str


@dataclass
class RelatorioPowerAutomate:
    dataverse_url: str
    verificacoes: list[Verificacao] = field(default_factory=list)

    def registra(self, nome: str, ok: bool, detalhe: str) -> None:
        self.verificacoes.append(Verificacao(nome, ok, detalhe))

    def render(self) -> str:
        linhas = [f"Dataverse: {self.dataverse_url}", "-" * 96]
        linhas += [f"{'OK ' if v.ok else 'FALHA'}  {v.nome:<22} {v.detalhe}" for v in self.verificacoes]
        return "\n".join(linhas)


def _explica(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPError):
        return f"rede: {type(exc).__name__} (o host do Dataverse respondeu?)"
    status = getattr(exc, "status", None)
    return f"{DIAGNOSTICO_HTTP[status]} ({exc})" if status in DIAGNOSTICO_HTTP else str(exc)


async def _consulta(dv: DataverseClient, rel: RelatorioPowerAutomate, nome: str, recurso: str,
                    params: dict[str, str] | None = None) -> dict | None:
    """GET que nunca levanta: a falha vira uma linha do relatorio com o diagnostico."""
    try:
        return await dv.get(recurso, params)
    except (DataverseError, httpx.HTTPError) as exc:
        rel.registra(nome, False, _explica(exc))
        return None


def _instante(texto: str | None) -> datetime | None:
    if not texto:
        return None
    return datetime.fromisoformat(texto.replace("Z", "+00:00"))


def atraso_de_ingestao(execucoes: list[dict]) -> list[float]:
    """Segundos entre o fim da execucao e o registro no Dataverse (`createdon - endtime`)."""
    atrasos = []
    for e in execucoes:
        fim, gravado = _instante(e.get("endtime")), _instante(e.get("createdon"))
        if fim and gravado:
            atrasos.append((gravado - fim).total_seconds())
    return atrasos


async def _acesso(dv: DataverseClient, rel: RelatorioPowerAutomate) -> bool:
    quem = await _consulta(dv, rel, "acesso (WhoAmI)", "WhoAmI")
    if quem is None:
        return False
    rel.registra("acesso (WhoAmI)", True, f"SP autenticado como usuario {quem.get('UserId')}")
    return True


async def _retencao(dv: DataverseClient, rel: RelatorioPowerAutomate) -> None:
    dados = await _consulta(dv, rel, "retencao", "organizations", {"$select": "flowruntimetoliveinseconds"})
    if dados is None:
        return
    orgs = dados.get("value") or [{}]
    ttl = orgs[0].get("flowruntimetoliveinseconds") or 0
    if ttl <= 0:
        rel.registra("retencao", False, "DESLIGADA (TTL 0): nenhuma execucao e gravada. Ligue no Passo D")
        return
    rel.registra("retencao", True, f"{ttl // 86400} dia(s) de historico")


async def _contar_fluxos(dv: DataverseClient, rel: RelatorioPowerAutomate, nome: str, categoria: int) -> None:
    dados = await _consulta(dv, rel, nome, "workflows", {
        "$select": "workflowid,name,statecode", "$filter": f"category eq {categoria}",
    })
    if dados is None:
        return
    fluxos = dados.get("value", [])
    ligados = sum(1 for f in fluxos if f.get("statecode") == ESTADO_LIGADO)
    rel.registra(nome, bool(fluxos), f"{len(fluxos)} fluxo(s), {ligados} ligado(s)")


async def _fluxos(dv: DataverseClient, rel: RelatorioPowerAutomate) -> None:
    await _contar_fluxos(dv, rel, "fluxos de nuvem", CATEGORIA_NUVEM)
    await _contar_fluxos(dv, rel, "fluxos de desktop", CATEGORIA_DESKTOP)


async def _execucoes(dv: DataverseClient, rel: RelatorioPowerAutomate) -> None:
    dados = await _consulta(dv, rel, "execucoes", "flowruns", {
        "$select": "name,status,starttime,endtime,createdon,workflowid,errorcode",
        "$orderby": "createdon desc", "$top": str(AMOSTRA_DE_EXECUCOES),
    })
    if dados is None:
        return
    execucoes = dados.get("value", [])
    if not execucoes:
        rel.registra("execucoes", False, "nenhuma execucao gravada: fluxos fora de solucao ou retencao recem-ligada")
        return
    status = sorted({str(e.get("status")) for e in execucoes})
    rel.registra("execucoes", True, f"{len(execucoes)} na amostra; valores de status: {', '.join(status)}")
    _registra_atraso(atraso_de_ingestao(execucoes), rel)


def _registra_atraso(atrasos: list[float], rel: RelatorioPowerAutomate, nome: str = "atraso de ingestao") -> None:
    if not atrasos:
        rel.registra(nome, False, "sem horario de fim e de registro na amostra para medir")
        return
    detalhe = (f"mediana {statistics.median(atrasos):.0f}s, pior {max(atrasos):.0f}s "
               f"(fim da execucao -> registro no Dataverse, {len(atrasos)} amostras)")
    rel.registra(nome, True, detalhe)


async def _execucoes_desktop(dv: DataverseClient, rel: RelatorioPowerAutomate) -> None:
    """Fluxo de desktop grava em `flowsession`, sempre -- nao depende de solucao."""
    dados = await _consulta(dv, rel, "execucoes de desktop", "flowsessions", {
        "$select": "statuscode,startedon,completedon,modifiedon,errorcode",
        "$orderby": "createdon desc", "$top": str(AMOSTRA_DE_EXECUCOES),
    })
    if dados is None:
        return
    sessoes = dados.get("value", [])
    if not sessoes:
        rel.registra("execucoes de desktop", False, "nenhuma sessao de fluxo de desktop gravada")
        return
    contagem = Counter(s.get("statuscode") for s in sessoes)
    resumo = ", ".join(f"{STATUS_DESKTOP.get(c, '?')}({c}): {n}" for c, n in contagem.most_common())
    erros = sorted({str(s["errorcode"]) for s in sessoes if s.get("errorcode")})
    rel.registra("execucoes de desktop", True, f"{len(sessoes)} na amostra; {resumo}")
    if erros:
        rel.registra("erros de desktop", True, f"codigos vistos: {', '.join(erros)}")
    fins = [{"endtime": s.get("completedon"), "createdon": s.get("modifiedon")} for s in sessoes]
    _registra_atraso(atraso_de_ingestao(fins), rel, "atraso desktop")


async def _perdas(dv: DataverseClient, rel: RelatorioPowerAutomate) -> None:
    dados = await _consulta(dv, rel, "perdas de ingestao", "flowevents", {
        "$select": "eventtype,eventcode,createdon", "$top": str(AMOSTRA_DE_EVENTOS),
        # So este tipo fala de execucao perdida; `Licensing` (desktop flow) e outros nao.
        "$filter": f"eventtype eq '{TIPO_PERDA_DE_INGESTAO}'",
    })
    if dados is None:
        return
    eventos = dados.get("value", [])
    codigos = sorted({str(e.get("eventcode")) for e in eventos if e.get("eventcode")})
    if codigos:
        rel.registra("perdas de ingestao", False, f"o Dataverse sinalizou: {', '.join(codigos)}")
        return
    rel.registra("perdas de ingestao", True, "nenhum aviso na tabela flowevent")


async def sondar(dv: DataverseClient) -> RelatorioPowerAutomate:
    """Nunca levanta. Sem acesso, para no primeiro passo: o resto daria o mesmo erro."""
    rel = RelatorioPowerAutomate(dataverse_url=dv.base_url)
    if not await _acesso(dv, rel):
        return rel
    for verificar in (_retencao, _fluxos, _execucoes, _execucoes_desktop, _perdas):
        await verificar(dv, rel)
    return rel


async def sondar_do_config(pa: PowerAutomateConfig) -> str:
    """Monta o cliente a partir do .env e devolve o relatorio pronto para o `doctor`."""
    if faltando := credenciais_ausentes(pa):
        return f"Dataverse: {pa.dataverse_url}\nFALHA  credencial ausente no .env: {', '.join(faltando)}"
    dv = cliente_do_config(pa)
    try:
        return (await sondar(dv)).render()
    finally:
        await dv.aclose()
