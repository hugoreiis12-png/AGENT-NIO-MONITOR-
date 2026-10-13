"""Quem e dono de uma stack e quem mexeu por ultimo, para os alertas de container."""

from __future__ import annotations

from typing import Any

import structlog

from .client import PortainerApiError, PortainerClient

log = structlog.get_logger(__name__)

LABEL_STACK = "com.docker.compose.project"


async def stacks_por_nome(client: PortainerClient) -> dict[str, dict[str, Any]]:
    """Falha aqui nao derruba o ciclo: o alerta sai, so sem o dono da stack."""
    try:
        return {str(s.get("Name")): s for s in await client.list_stacks()}
    except PortainerApiError as exc:
        log.warning("stacks_indisponiveis_alerta_sem_dono", erro=str(exc))
        return {}


def stack_do_container(row: dict[str, Any]) -> str | None:
    nome = (row.get("Labels") or {}).get(LABEL_STACK)
    return str(nome) if nome else None


def dono_da_stack(nome: str | None, stacks: dict[str, dict[str, Any]]) -> dict[str, str | None]:
    stack = stacks.get(nome) if nome else None
    if stack is None:
        return {}
    return {"stack": nome, "autor": stack.get("UpdatedBy") or None, "responsavel_stack": stack.get("CreatedBy") or None}
