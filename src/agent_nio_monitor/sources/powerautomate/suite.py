"""Monta o cliente e o coletor do Power Automate a partir da config e do `.env`."""

from __future__ import annotations

import os

import structlog

from ...config.schema import AppConfig, PowerAutomateConfig
from ...store.repository import Repository
from .client import DataverseClient
from .desktop import DesktopFlowCollector

log = structlog.get_logger(__name__)


def credenciais_ausentes(pa: PowerAutomateConfig) -> list[str]:
    return [nome for nome in (pa.tenant_id_env, pa.client_id_env, pa.client_secret_env) if not os.environ.get(nome)]


def cliente_do_config(pa: PowerAutomateConfig) -> DataverseClient:
    return DataverseClient(
        pa.dataverse_url, tenant_id=os.environ[pa.tenant_id_env], client_id=os.environ[pa.client_id_env],
        client_secret=os.environ[pa.client_secret_env], timeout=pa.timeout_seconds,
    )


def build_power_automate(cfg: AppConfig, repo: Repository) -> DesktopFlowCollector | None:
    """None sem config ou sem credencial: so o ciclo do Power Automate fica de lado (mesmo criterio do Portainer)."""
    pa = cfg.active.powerautomate
    if pa is None:
        return None
    if faltando := credenciais_ausentes(pa):
        log.warning("power_automate_sem_credencial_ciclo_desligado", faltando=faltando)
        return None
    return DesktopFlowCollector(cliente_do_config(pa), repo)
