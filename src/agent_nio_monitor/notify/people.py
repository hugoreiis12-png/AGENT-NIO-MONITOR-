"""Leitura de `config/people.yaml`: responsaveis por projeto e usuarios do Portainer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

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
