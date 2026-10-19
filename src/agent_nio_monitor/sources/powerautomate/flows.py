"""Fluxo de desktop novo no ambiente (tabela `workflows`, category 6): aviso verde de criacao.

32 fluxos medidos em 14/10/2026 -- a listagem inteira e pequena, entao cada ciclo le todas e
compara com os ids conhecidos (`resource`, kind `desktop-flow`). Primeira leitura so semeia.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from ...notify.templates import formatar_horas
from ...store.repository import EventRecord, Repository, new_event_id, now_utc
from ..base import instante
from .client import DataverseClient, DataverseError
from .desktop import PREFIXO_DA_CHAVE, ROTULO

log = structlog.get_logger(__name__)

KIND = "desktop-flow"
CATEGORIA_DESKTOP = 6


class DesktopFlowCatalog:
    source = "powerautomate"

    def __init__(self, client: DataverseClient, repo: Repository) -> None:
        self.client, self.repo = client, repo

    async def novos(self) -> list[EventRecord]:
        try:
            dados = await self.client.get("workflows", {
                "$select": "workflowid,name,createdon,_ownerid_value",
                "$filter": f"category eq {CATEGORIA_DESKTOP}",
            }, anotacoes=True)
        except (DataverseError, httpx.HTTPError) as exc:
            log.warning("catalogo_de_fluxos_indisponivel", erro=str(exc))
            return []
        conhecidos = {r["external_id"] for r in self.repo.list_resources(self.source, KIND)}
        semente = not conhecidos
        eventos = []
        for fluxo in dados.get("value", []):
            fid = str(fluxo.get("workflowid") or "")
            if not fid or fid in conhecidos:
                continue
            self.repo.upsert_resource(source=self.source, kind=KIND, external_id=fid, name=fluxo.get("name"))
            if not semente:
                eventos.append(evento_fluxo_criado(fluxo))
        return eventos


def evento_fluxo_criado(fluxo: dict[str, Any]) -> EventRecord:
    nome = str(fluxo.get("name") or fluxo.get("workflowid"))
    dono = fluxo.get(f"_ownerid_value{ROTULO}")
    return EventRecord(
        id=new_event_id(), ts=now_utc(), source="powerautomate", event_type="desktop_flow_created", severity="low",
        title=f"Fluxo de RPA criado: {nome}", resource_id=f"{PREFIXO_DA_CHAVE}{fluxo.get('workflowid')}",
        detail={
            "fluxo_id": fluxo.get("workflowid"), "workflow_nome": nome, "recurso": nome,
            "responsavel_fluxo": dono, "ultima": formatar_horas(instante(fluxo.get("createdon"))),
            "detalhe": f"Fluxo da area de trabalho novo no ambiente, criado por {dono or 'responsavel desconhecido'}.",
        },
    )
