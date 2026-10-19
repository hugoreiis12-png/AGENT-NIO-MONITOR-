"""Reconfere se o problema de um alerta pendente ja passou.

Usa o estado do ULTIMO ciclo de cada coletor (sem nova chamada de API), exceto a
execucao de workflow, que precisa de 1 consulta. Devolve o motivo do descarte ou None
quando o problema continua. Eventos que sao FATOS (workflow alterado/criado, stack
alterada) nao tem o que "resolver": sempre seguem para o envio.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog

from ..config.schema import PollingConfig, ThresholdsConfig
from ..sources.n8n.client import N8NApiError
from ..sources.powerautomate.desktop import STATUS_SUCESSO as STATUS_SUCESSO_DESKTOP
from ..store.repository import EventRecord

log = structlog.get_logger(__name__)

MARGEM_SEGUNDOS = 5.0


def espera_em_segundos(event_type: str, polling: PollingConfig) -> float:
    """Quanto o alerta espera: 1 ciclo da fonte, para a reconferencia ver dado novo.

    Com teto: espera + 1 tick da fila nao passa de `queue_max_delay_seconds`. Fonte
    de 60 s e reconferida antes do proximo ciclo dela -- o preco do alerta em 50 s.
    Fatos (nada a resolver) saem sem espera."""
    ciclo = {
        "execution_failed": polling.execution_errors_seconds,
        "desktop_flow_failed": polling.execution_errors_seconds,
        "desktop_flow_handled_errors": polling.execution_errors_seconds,
        "node_not_executed": polling.execution_errors_seconds,
        "node_error_hidden": polling.execution_errors_seconds,
        "container_stopped": polling.container_state_seconds,
        "container_unhealthy": polling.container_state_seconds,
        "container_removed": polling.container_state_seconds,
        "container_restart_loop": polling.container_state_seconds,
        "container_resource_high": polling.container_stats_seconds,
        "workflow_activated": polling.workflow_full_seconds,
        "workflow_deactivated": polling.workflow_full_seconds,
    }.get(event_type)
    if not ciclo:
        return 0.0
    teto = polling.queue_max_delay_seconds - polling.queue_seconds
    return min(ciclo + MARGEM_SEGUNDOS, teto)


def _instante(texto: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(texto).replace("Z", "+00:00"))
    except ValueError:
        return None


class ResolutionChecker:
    def __init__(self, *, containers: Any, resources: Any, n8n: Any, repo: Any, limites: ThresholdsConfig,
                 desktop: Any = None, deep: Any = None) -> None:
        self.containers, self.resources, self.n8n, self.repo, self.limites = containers, resources, n8n, repo, limites
        self.desktop, self.deep = desktop, deep

    async def motivo(self, evento: EventRecord) -> str | None:
        """Motivo do descarte (o problema passou) ou None (continua: enviar)."""
        verificador = getattr(self, f"_{evento.event_type}", None)
        return await verificador(evento) if verificador else None

    async def _container_stopped(self, e: EventRecord) -> str | None:
        c = self.containers.estado_atual(e.resource_id) if self.containers else None
        return "container voltou a rodar" if c and c["state"] == "running" else None

    async def _container_unhealthy(self, e: EventRecord) -> str | None:
        c = self.containers.estado_atual(e.resource_id) if self.containers else None
        voltou = c and c["state"] == "running" and c["health"] != "unhealthy"
        return "healthcheck voltou ao normal" if voltou else None

    async def _container_removed(self, e: EventRecord) -> str | None:
        if not self.containers:
            return None
        presente = self.containers.nome_presente(e.detail.get("endpoint_id"), e.detail.get("recurso"))
        return "container voltou a existir" if presente else None

    async def _container_restart_loop(self, e: EventRecord) -> str | None:
        c = self.containers.estado_atual(e.resource_id) if self.containers else None
        return "container estabilizou" if c and c["state"] == "running" and not c["recente"] else None

    async def _container_resource_high(self, e: EventRecord) -> str | None:
        amostra = self.resources.ultima_amostra.get(e.resource_id) if self.resources else None
        if not amostra or amostra == (None, None):
            return None
        cpu, mem = amostra
        normal = (cpu is None or cpu < self.limites.cpu_host_pct) and (mem is None or mem < self.limites.mem_pct)
        return "consumo voltou ao normal" if normal else None

    async def _execution_failed(self, e: EventRecord) -> str | None:
        falha = _instante(e.detail.get("ultima_falha_ts"))
        try:
            ultima = await self.n8n.last_execution(str(e.detail.get("workflow_id")))
        except N8NApiError as exc:
            log.warning("reconferencia_de_execucao_falhou", erro=str(exc))
            return None
        inicio = _instante((ultima or {}).get("startedAt"))
        if ultima and ultima.get("status") == "success" and falha and inicio and inicio > falha:
            return "execucao posterior com sucesso"
        return None

    async def _desktop_flow_failed(self, e: EventRecord) -> str | None:
        """Ultimo estado visto pelo coletor de RPA: sucesso DEPOIS da falha resolve."""
        estado = self.desktop.estado.get(e.resource_id) if self.desktop else None
        falha = _instante(e.detail.get("ultima_falha_ts"))
        if estado and falha and estado[0] == STATUS_SUCESSO_DESKTOP and estado[1] > falha:
            return "execucao posterior do RPA com sucesso"
        return None

    async def _workflow_toggle(self, e: EventRecord) -> str | None:
        snapshot = self.repo.last_workflow_snapshot(str(e.detail.get("workflow_id")))
        voltou = snapshot is not None and bool(snapshot["active"]) != bool(e.detail.get("ativo"))
        return "estado do workflow voltou ao anterior" if voltou else None

    _workflow_activated = _workflow_toggle
    _workflow_deactivated = _workflow_toggle

    def _analise_posterior(self, e: EventRecord) -> Any:
        """Ultima execucao de sucesso lida do workflow, se for DEPOIS da que gerou o alerta."""
        analise = self.deep.caminhos.ultimo.get(e.resource_id) if self.deep else None
        quando = _instante(e.detail.get("ultima_ts"))
        return analise if analise and quando and analise.inicio > quando else None

    async def _node_not_executed(self, e: EventRecord) -> str | None:
        analise = self._analise_posterior(e)
        voltaram = analise and set(e.detail.get("nos") or []) <= analise.rodaram
        return "os nos voltaram a rodar na execucao seguinte" if voltaram else None

    async def _node_error_hidden(self, e: EventRecord) -> str | None:
        analise = self._analise_posterior(e)
        limpo = analise and not set(e.detail.get("nos") or []) & analise.com_erro
        return "execucao seguinte sem erro nesses nos" if limpo else None

    async def _desktop_flow_handled_errors(self, e: EventRecord) -> str | None:
        acoes = self.deep.acoes if self.deep else None
        ultimo = acoes.ultimo_tratado.get(e.resource_id) if acoes else None
        quando = _instante(e.detail.get("ultima_ts"))
        limpo = ultimo and quando and ultimo[0] > quando and not ultimo[1]
        return "sessao seguinte do RPA sem erro tratado" if limpo else None
