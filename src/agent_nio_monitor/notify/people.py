"""Leitura de `config/people.yaml`: responsaveis por projeto e usuarios do Portainer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
import yaml

log = structlog.get_logger(__name__)

PEOPLE_PATH = Path(__file__).resolve().parents[3] / "config" / "people.yaml"


def _load() -> dict[str, Any]:
    if not PEOPLE_PATH.exists():
        return {}
    return yaml.safe_load(PEOPLE_PATH.read_text(encoding="utf-8")) or {}


def people_by_project() -> dict[str, list[str]]:
    return _load().get("projects") or {}


def portainer_users() -> dict[str, str]:
    return _load().get("usuarios") or {}


def exclusive_by_workflow() -> dict[str, list[str]]:
    return _load().get("workflows") or {}


def exclusive_by_source() -> dict[str, list[str]]:
    """`fontes:` -- todo alerta daquela fonte vai so para a lista (ex.: powerautomate)."""
    return _load().get("fontes") or {}


def global_fallback() -> list[str]:
    """Enderecos de seguranca para quando ninguem e resolvido.

   Ultimo degrau da cadeia do `RecipientResolver`. Sem isto, um projeto fora
    de `projects:` termina em `Resolution(emails=[])` e o alerta e gravado como
    `failed` e nunca sai -- o pior modo de falha de um monitor, e o que o
    modulo do resolver chama de "monitor virar furniture".

    Deliberadamente VAZIO no `people.yaml`: quem decide e o dono, porque isto e
    um endereco de pessoa, nao um default. Vazio here e falha barulhenta e
    rastreavel; ausente, nem chega a ser lido.
    """
    return _load().get("global") or []


def fallback_de_emergencia() -> list[str]:
    """Ultimo degrau do `RecipientResolver`, lido uma vez no boot.

    Fica numa funcao propria porque a ausencia de destinatario precisa ser
    dita UMA vez, no boot, e nao a cada alerta perdido.
    """
    fallback = global_fallback()
    if not fallback:
        log.warning(
            "people_yaml_sem_fallback_global",
            efeito="projeto fora de people.yaml:projects termina em 0 destinatario e o alerta nao sai",
            como_resolver="preencher a chave `global:` em config/people.yaml com um endereco de plantao",
        )
    return fallback
