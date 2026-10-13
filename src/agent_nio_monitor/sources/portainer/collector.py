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

from ...redaction import redact_text
from ...store.repository import EventRecord, Repository, new_event_id, now_utc
from ..base import CollectorResult
from .client import PortainerApiError, PortainerClient, parse_container

log = structlog.get_logger(__name__)

LABEL_STACK = "com.docker.compose.project"
LOG_LINHAS = 40
LOG_MAX_CHARS = 1200


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

        stacks = await self._stacks_por_nome()
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
                eventos.extend(await self._processar_container(int(endpoint_id), row, stacks))

        return CollectorResult(collected=total, extra={"eventos": eventos})

    async def _stacks_por_nome(self) -> dict[str, dict]:
        """Falha aqui nao derruba o ciclo: o alerta sai, so sem o dono da stack."""
        try:
            return {str(s.get("Name")): s for s in await self.client.list_stacks()}
        except PortainerApiError as exc:
            log.warning("stacks_indisponiveis_alerta_sem_dono", erro=str(exc))
            return {}

    async def _processar_container(
        self, endpoint_id: int, row: dict, stacks: dict[str, dict]
    ) -> list[EventRecord]:
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
            eventos.append(self._evento_parado(container_id, nome, container, _dono(row, stacks)))

        ficou_unhealthy_agora = container["health"] == "unhealthy" and (
            anterior is None or anterior.get("health") != "unhealthy"
        )
        if ficou_unhealthy_agora:
            eventos.append(self._evento_unhealthy(container_id, nome, container, _dono(row, stacks)))

        if eventos:
            logs = await self._log_recente(endpoint_id, container_id)
            for e in eventos:
                e.detail["logs"] = logs
        self.repo.record_container_observation(
            container_id,
            now_utc(),
            state=container["state"],
            health=container["health"],
            restart_count=container["restart_count"],
        )
        return eventos

    async def _log_recente(self, endpoint_id: int, container_id: str) -> str | None:
        """Melhor esforco: sem log o alerta sai igual. Redigido antes de sair daqui."""
        try:
            texto = await self.client.container_logs(endpoint_id, container_id, tail=LOG_LINHAS)
        except PortainerApiError as exc:
            log.warning("log_do_container_indisponivel", container_id=container_id, erro=str(exc))
            return None
        return redact_text(texto.strip())[-LOG_MAX_CHARS:] or None

    def _evento_parado(self, container_id: str, nome: str, container: dict, dono: dict) -> EventRecord:
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
                **dono,
            },
        )

    def _evento_unhealthy(self, container_id: str, nome: str, container: dict, dono: dict) -> EventRecord:
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
                **dono,
            },
        )


def _dono(row: dict, stacks: dict[str, dict]) -> dict[str, str | None]:
    nome = (row.get("Labels") or {}).get(LABEL_STACK)
    stack = stacks.get(str(nome)) if nome else None
    if stack is None:
        return {}
    return {"stack": str(nome), "autor": stack.get("UpdatedBy"), "responsavel_stack": stack.get("CreatedBy")}
