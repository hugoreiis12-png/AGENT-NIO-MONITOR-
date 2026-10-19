"""Mudanca de stack (compose) -- so alerta quando o refactor e relevante.

Sinal barato primeiro (`UpdateDate`/`UpdatedBy` da listagem); o arquivo so e
buscado quando esse sinal muda. Abaixo do limite de proporcao, grava e segue.
"""

from __future__ import annotations

from typing import Any

import structlog

from ...store.repository import EventRecord, Repository, new_event_id, now_utc
from ..base import CollectorResult, ainda_em_edicao, instante
from .client import PortainerApiError, PortainerClient
from .compose_diff import normalizar, proporcao_alterada, resumo_mudancas

log = structlog.get_logger(__name__)


class StackCollector:
    source = "portainer"

    def __init__(
        self, client: PortainerClient, repo: Repository, *, ratio_minimo: float = 0.20, quieto_minutos: int = 0,
    ) -> None:
        self.client = client
        self.repo = repo
        self.ratio_minimo = ratio_minimo
        self.quieto_minutos = quieto_minutos

    async def check_changes(self) -> CollectorResult:
        try:
            stacks = await self.client.list_stacks()
        except PortainerApiError as exc:
            log.error("listagem_de_stacks_falhou", erro=str(exc))
            return CollectorResult(error=str(exc))
        conhecidas = {r["external_id"]: r.get("meta") or {} for r in self.repo.list_resources("portainer", "stack")}
        baseline = bool(conhecidas)
        eventos = []
        for stack in stacks:
            evento = await self._processar(stack, conhecidas.get(str(stack.get("Id"))), baseline)
            if evento is not None:
                eventos.append(evento)
        return CollectorResult(collected=len(stacks), extra={"eventos": eventos})

    async def _processar(self, stack: dict[str, Any], anterior: dict[str, Any] | None, baseline: bool) -> EventRecord | None:
        assinatura = {"update_date": stack.get("UpdateDate"), "updated_by": stack.get("UpdatedBy")}
        if anterior and all(anterior.get(k) == v for k, v in assinatura.items()):
            return None
        if anterior and ainda_em_edicao(instante(stack.get("UpdateDate")), self.quieto_minutos, now_utc()):
            return None  # redeploy em andamento: compara com a base antiga quando assentar
        try:
            conteudo = normalizar(await self.client.stack_file(stack["Id"]))
        except PortainerApiError as exc:
            log.warning("compose_da_stack_indisponivel", stack=stack.get("Name"), erro=str(exc))
            return None
        self.repo.upsert_resource(
            source="portainer", kind="stack", external_id=str(stack["Id"]), name=stack.get("Name"),
            meta={**assinatura, "content": conteudo},
        )
        if not baseline or anterior is None:
            # Stack nova chega como `container_created` (verde), agrupado pela stack: um aviso so.
            return None
        antes = (anterior or {}).get("content", "")
        proporcao = proporcao_alterada(antes, conteudo)
        if proporcao <= self.ratio_minimo:
            log.info("stack_alterada_abaixo_do_limite", stack=stack.get("Name"), proporcao=round(proporcao, 2))
            return None
        return self._evento(stack, antes, conteudo, proporcao)

    def _evento(self, stack: dict[str, Any], antes: str, depois: str, proporcao: float) -> EventRecord:
        nome = str(stack.get("Name"))
        pct = round(proporcao * 100)
        resumo = resumo_mudancas(antes, depois)
        return EventRecord(
            id=new_event_id(), ts=now_utc(), source=self.source, event_type="stack_changed", severity="high",
            title=f"Stack {nome} alterada ({pct}% do compose)", resource_id=str(stack.get("Id")),
            detail={
                "recurso": nome, "workflow_nome": nome, "stack": nome,
                "stack_id": stack.get("Id"), "endpoint_id": stack.get("EndpointId"),
                "autor": stack.get("UpdatedBy") or None, "responsavel_stack": stack.get("CreatedBy") or None,
                "resumo_diff": resumo, "detalhe": f"{pct}% do compose mudou.",
            },
        )
