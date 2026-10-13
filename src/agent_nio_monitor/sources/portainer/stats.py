"""CPU e memoria por container -- so alerta consumo ALTO E SUSTENTADO.

CPU e a media entre duas coletas (o `one-shot` do Docker nao traz amostra
anterior), entao a primeira passada so forma a linha de base. Um pico isolado
nunca alerta: o limite precisa ser excedido por N coletas seguidas.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from ...store.repository import EventRecord, Repository, new_event_id, now_utc
from ..base import CollectorResult
from .client import PortainerApiError, PortainerClient, parse_container
from .donos import dono_da_stack, stack_do_container, stacks_por_nome
from .metrics import cpu_do_host_pct, memoria_pct, totais_de_cpu

log = structlog.get_logger(__name__)

CONCORRENCIA = 5


class ResourceCollector:
    source = "portainer"

    def __init__(self, client: PortainerClient, repo: Repository, *, cpu_host_pct: float = 80.0,
                 mem_pct: float = 90.0, sustained_samples: int = 3) -> None:
        self.client = client
        self.repo = repo
        self.cpu_limite, self.mem_limite, self.sustentacao = cpu_host_pct, mem_pct, sustained_samples
        self._cpu_anterior: dict[str, tuple[int, int]] = {}
        self._altas: dict[str, int] = {}

    async def check_resources(self) -> CollectorResult:
        try:
            endpoints = await self.client.list_endpoints()
        except PortainerApiError as exc:
            log.error("listagem_de_endpoints_falhou", erro=str(exc))
            return CollectorResult(error=str(exc))
        stacks = await stacks_por_nome(self.client)
        semaforo = asyncio.Semaphore(CONCORRENCIA)
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
            rodando = [r for r in rows if (r.get("State") or "").lower() == "running"]
            amostras = await asyncio.gather(*(self._amostra(int(endpoint["Id"]), r, semaforo) for r in rodando))
            for row, (cpu, mem) in zip(rodando, amostras, strict=True):
                total += 1
                evento = self._avaliar(row, cpu, mem, stacks, int(endpoint["Id"]))
                if evento is not None:
                    eventos.append(evento)
        return CollectorResult(collected=total, extra={"eventos": eventos})

    async def _amostra(self, endpoint_id: int, row: dict, semaforo: asyncio.Semaphore) -> tuple[float | None, float | None]:
        container_id = parse_container(row)["container_id"]
        async with semaforo:
            try:
                stats = await self.client.container_stats(endpoint_id, container_id)
            except PortainerApiError as exc:
                log.warning("stats_indisponiveis", container_id=container_id, erro=str(exc))
                return None, None
        return self._cpu(container_id, stats), memoria_pct(stats)

    def _cpu(self, container_id: str, stats: dict[str, Any]) -> float | None:
        atual = totais_de_cpu(stats)
        anterior = self._cpu_anterior.get(container_id)
        if atual is not None:
            self._cpu_anterior[container_id] = atual
        if atual is None or anterior is None:
            return None
        return cpu_do_host_pct(anterior, atual)

    def _avaliar(
        self, row: dict, cpu: float | None, mem: float | None, stacks: dict[str, dict], endpoint_id: int,
    ) -> EventRecord | None:
        container = parse_container(row)
        container_id = container["container_id"]
        self.repo.record_container_observation(
            container_id, now_utc(), state=container["state"], health=container["health"], mem_pct=mem, cpu_pct=cpu,
        )
        excessos = []
        if cpu is not None and cpu >= self.cpu_limite:
            excessos.append(f"CPU em {cpu:.0f}% do host (limite {self.cpu_limite:.0f}%)")
        if mem is not None and mem >= self.mem_limite:
            excessos.append(f"memoria em {mem:.0f}% do limite (limite de alerta {self.mem_limite:.0f}%)")
        if not excessos:
            self._altas.pop(container_id, None)
            return None
        self._altas[container_id] = self._altas.get(container_id, 0) + 1
        if self._altas[container_id] != self.sustentacao:
            return None
        return self._evento(container, "; ".join(excessos), dono_da_stack(stack_do_container(row), stacks), endpoint_id)

    def _evento(self, container: dict, excessos: str, dono: dict, endpoint_id: int) -> EventRecord:
        nome = container["name"]
        detalhe = f"Consumo acima do limite por {self.sustentacao} coletas seguidas: {excessos}."
        return EventRecord(
            id=new_event_id(), ts=now_utc(), source=self.source, event_type="container_resource_high",
            severity="high", title=f"Consumo alto: {nome}", resource_id=container["container_id"],
            detail={"recurso": nome, "workflow_nome": nome, "detalhe": detalhe, "endpoint_id": endpoint_id, **dono},
        )
