"""Os coletores do Portainer compartilham um unico cliente HTTP."""

from __future__ import annotations

import os

import structlog

from ...config.schema import AppConfig
from ...store.repository import Repository
from .client import PortainerClient
from .collector import ContainerCollector
from .stacks import StackCollector
from .stats import ResourceCollector

log = structlog.get_logger(__name__)


def build_portainer(
    cfg: AppConfig, repo: Repository
) -> tuple[ContainerCollector, StackCollector, ResourceCollector] | None:
    """None quando Portainer nao esta configurado ou sem credencial -- mesmo
    criterio do `doctor`. Travar o boot por falta de um recurso opcional seria a
    borda errada: so os ciclos do Portainer ficam de lado."""
    env = cfg.active.portainer
    if env is None:
        return None
    api_key = os.environ.get(env.api_key_env, "")
    if not api_key:
        log.warning("portainer_sem_credencial_ciclos_do_portainer_desligados")
        return None
    t = cfg.policy.thresholds
    client = PortainerClient(env.url, api_key, verify_ssl=env.verify_ssl, timeout=env.timeout_seconds)
    return (
        ContainerCollector(client, repo, loop_count=t.restart_loop_count, loop_window_minutes=t.restart_loop_window_minutes),
        StackCollector(
            client, repo, ratio_minimo=t.stack_change_ratio,
            quieto_minutos=cfg.policy.aggregation.edicao_quieta_minutos,
        ),
        ResourceCollector(client, repo, cpu_host_pct=t.cpu_host_pct, mem_pct=t.mem_pct, sustained_samples=t.sustained_samples),
    )
