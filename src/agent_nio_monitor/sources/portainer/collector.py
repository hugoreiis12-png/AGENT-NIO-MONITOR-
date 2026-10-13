"""Liga o cliente Portainer (T6) ao pipeline de evento.

So o estado (`list_containers`, uma chamada em lote por endpoint) entra no
ciclo rapido. `restart_count` fica sempre None aqui -- so o `inspect_container`
(por container) traz isso, e chamar inspect em todo ciclo custaria o rate
limit todo. Deteccao de restart-loop (`container_restart_loop`, ja na policy)
depende desse dado e por isso NAO esta implementada ainda; precisa de um
terceiro ciclo que chame inspect sob um orcamento proprio.
"""

from __future__ import annotations

import structlog

from ...store.repository import EventRecord, Repository, new_event_id, now_utc
from ..base import CollectorResult
from .client import PortainerApiError, PortainerClient, parse_container

log = structlog.get_logger(__name__)


class ContainerCollector:
    source = "portainer"

    def __init__(self, client: PortainerClient, repo: Repository) -> None:
        self.client = client
        self.repo = repo

    async def check_state(self) -> CollectorResult:
        eventos: list[EventRecord] = []
        try:
            endpoints = await self.client.list_endpoints()
        except PortainerApiError as exc:
            log.error("listagem_de_endpoints_falhou", erro=str(exc))
            return CollectorResult(error=str(exc))

        total = 0
        for endpoint in endpoints:
            endpoint_id = endpoint.get("Id")
            if endpoint_id is None:
                continue
            try:
                containers = await self.client.list_containers(int(endpoint_id))
            except PortainerApiError as exc:
                log.error("listagem_de_containers_falhou", endpoint_id=endpoint_id, erro=str(exc))
                continue
            for row in containers:
                total += 1
                eventos.extend(self._processar_container(row))

        return CollectorResult(collected=total, extra={"eventos": eventos})

    def _processar_container(self, row: dict) -> list[EventRecord]:
        container = parse_container(row)
        container_id = container["container_id"]
        nome = container["name"]
        anterior = self.repo.last_container_observation(container_id)

        eventos: list[EventRecord] = []
        parou_agora = (
            anterior is not None
            and anterior.get("state") == "running"
            and container["state"] != "running"
        )
        if parou_agora:
            eventos.append(self._evento_parado(container_id, nome, container))

        ficou_unhealthy_agora = container["health"] == "unhealthy" and (
            anterior is None or anterior.get("health") != "unhealthy"
        )
        if ficou_unhealthy_agora:
            eventos.append(self._evento_unhealthy(container_id, nome, container))

        self.repo.record_container_observation(
            container_id,
            now_utc(),
            state=container["state"],
            health=container["health"],
            restart_count=container["restart_count"],
        )
        return eventos

    def _evento_parado(self, container_id: str, nome: str, container: dict) -> EventRecord:
        return EventRecord(
            id=new_event_id(),
            ts=now_utc(),
            source=self.source,
            event_type="container_stopped",
            severity="critical",
            title=f"Container parado: {nome}",
            resource_id=container_id,
            detail={
                "recurso": nome,
                "workflow_nome": nome,
                "detalhe": f"Estado atual: {container['state']} ({container['status']})",
            },
        )

    def _evento_unhealthy(self, container_id: str, nome: str, container: dict) -> EventRecord:
        return EventRecord(
            id=new_event_id(),
            ts=now_utc(),
            source=self.source,
            event_type="container_unhealthy",
            severity="critical",
            title=f"Container unhealthy: {nome}",
            resource_id=container_id,
            detail={
                "recurso": nome,
                "workflow_nome": nome,
                "detalhe": f"Healthcheck falhando. Estado: {container['status']}",
            },
        )
