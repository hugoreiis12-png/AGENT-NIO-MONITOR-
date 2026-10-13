"""Estado dos containers (T6): parou, ficou unhealthy, foi removido, entrou em loop.

A listagem (`list_containers`, 1 chamada por endpoint) alimenta tudo. O `inspect`
(unico que traz RestartCount) so e chamado para container que acabou de reiniciar
e sob um teto por ciclo -- nunca para os 35 a cada minuto.
"""

from __future__ import annotations

from datetime import timedelta

import structlog

from ...redaction import redact_text
from ...store.repository import EventRecord, Repository, new_event_id, now_utc
from ..base import CollectorResult
from .client import PortainerApiError, PortainerClient, parse_container
from .donos import dono_da_stack, stack_do_container, stacks_por_nome
from .lifecycle import LifecycleTracker
from .metrics import eh_candidato_a_loop

log = structlog.get_logger(__name__)

LOG_LINHAS = 40
LOG_MAX_CHARS = 1200
MAX_INSPECT_POR_CICLO = 10


class ContainerCollector:
    source = "portainer"

    def __init__(
        self, client: PortainerClient, repo: Repository, *,
        loop_count: int = 3, loop_window_minutes: int = 10,
    ) -> None:
        self.client = client
        self.repo = repo
        self.tracker = LifecycleTracker(loop_count=loop_count, loop_window=timedelta(minutes=loop_window_minutes))
        self._inspects_restantes = MAX_INSPECT_POR_CICLO

    async def check_state(self) -> CollectorResult:
        try:
            endpoints = await self.client.list_endpoints()
        except PortainerApiError as exc:
            log.error("listagem_de_endpoints_falhou", erro=str(exc))
            return CollectorResult(error=str(exc))

        stacks = await stacks_por_nome(self.client)
        self._inspects_restantes = MAX_INSPECT_POR_CICLO
        eventos: list[EventRecord] = []
        total = 0
        for endpoint in endpoints:
            if endpoint.get("Id") is None:
                continue
            try:
                rows = await self.client.list_containers(int(endpoint["Id"]))
            except PortainerApiError as exc:
                log.error("listagem_de_containers_falhou", endpoint_id=endpoint["Id"], erro=str(exc))
                continue
            total += len(rows)
            eventos.extend(await self._processar_endpoint(int(endpoint["Id"]), rows, stacks))
        return CollectorResult(collected=total, extra={"eventos": eventos})

    async def _processar_endpoint(self, endpoint_id: int, rows: list[dict], stacks: dict[str, dict]) -> list[EventRecord]:
        eventos: list[EventRecord] = []
        for row in rows:
            eventos.extend(await self._processar_container(endpoint_id, row, stacks))
        atuais = {parse_container(r)["name"]: {"stack": stack_do_container(r)} for r in rows}
        for nome, info in self.tracker.removidos(endpoint_id, atuais):
            removido = self._evento_removido(nome, dono_da_stack(info["stack"], stacks))
            removido.detail["endpoint_id"] = endpoint_id
            eventos.append(removido)
        return eventos

    async def _processar_container(self, endpoint_id: int, row: dict, stacks: dict[str, dict]) -> list[EventRecord]:
        container = parse_container(row)
        container_id, nome = container["container_id"], container["name"]
        dono = dono_da_stack(stack_do_container(row), stacks)
        eventos = self._transicoes(container, self.repo.last_container_observation(container_id), dono)
        reinicios = await self._contagem_de_reinicios(endpoint_id, row, container_id)
        if reinicios is not None and (n := self.tracker.reinicios_na_janela(container_id, reinicios, now_utc())):
            eventos.append(self._evento_loop(container_id, nome, n, reinicios, dono))
        if eventos:
            logs = await self._log_recente(endpoint_id, container_id)
            for e in eventos:
                e.detail["logs"] = logs
                e.detail["endpoint_id"] = endpoint_id
        self.repo.record_container_observation(
            container_id, now_utc(), state=container["state"], health=container["health"], restart_count=reinicios,
        )
        return eventos

    def _transicoes(self, container: dict, anterior: dict | None, dono: dict) -> list[EventRecord]:
        container_id, nome = container["container_id"], container["name"]
        eventos = []
        if anterior is not None and anterior.get("state") == "running" and container["state"] != "running":
            eventos.append(self._evento(
                "container_stopped", container_id, f"Container parado: {nome}", nome,
                f"Estado atual: {container['state']} ({container['status']})", dono))
        if container["health"] == "unhealthy" and (anterior is None or anterior.get("health") != "unhealthy"):
            eventos.append(self._evento(
                "container_unhealthy", container_id, f"Container unhealthy: {nome}", nome,
                f"Healthcheck falhando. Estado: {container['status']}", dono))
        return eventos

    async def _contagem_de_reinicios(self, endpoint_id: int, row: dict, container_id: str) -> int | None:
        if not eh_candidato_a_loop(row) or self._inspects_restantes <= 0:
            return None
        self._inspects_restantes -= 1
        try:
            detalhe = await self.client.inspect_container(endpoint_id, container_id)
        except PortainerApiError as exc:
            log.warning("inspect_falhou", container_id=container_id, erro=str(exc))
            return None
        contagem = detalhe.get("RestartCount")
        return int(contagem) if contagem is not None else None

    async def _log_recente(self, endpoint_id: int, container_id: str) -> str | None:
        """Melhor esforco: sem log o alerta sai igual. Redigido antes de sair daqui."""
        try:
            texto = await self.client.container_logs(endpoint_id, container_id, tail=LOG_LINHAS)
        except PortainerApiError as exc:
            log.warning("log_do_container_indisponivel", container_id=container_id, erro=str(exc))
            return None
        return redact_text(texto.strip())[-LOG_MAX_CHARS:] or None

    def _evento(self, tipo: str, container_id: str, titulo: str, nome: str, detalhe: str, dono: dict) -> EventRecord:
        return EventRecord(
            id=new_event_id(), ts=now_utc(), source=self.source, event_type=tipo, severity="critical",
            title=titulo, resource_id=container_id,
            detail={"recurso": nome, "workflow_nome": nome, "detalhe": detalhe, **dono},
        )

    def _evento_loop(self, container_id: str, nome: str, na_janela: int, total: int, dono: dict) -> EventRecord:
        janela = int(self.tracker.loop_window.total_seconds() // 60)
        detalhe = f"{na_janela} reinicios em {janela} min (total acumulado: {total})."
        return self._evento("container_restart_loop", container_id, f"Loop de reinicio: {nome}", nome, detalhe, dono)

    def _evento_removido(self, nome: str, dono: dict) -> EventRecord:
        detalhe = "O container nao existe mais no Docker (nao esta nem parado)."
        return self._evento("container_removed", nome, f"Container removido: {nome}", nome, detalhe, dono)