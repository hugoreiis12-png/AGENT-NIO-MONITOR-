"""Monta o cliente do n8n a partir da config e do `.env` (mesmo formato do Portainer e do Power Automate)."""

from __future__ import annotations

import os

from ...config.schema import AppConfig
from .client import N8NClient


def build_n8n(cfg: AppConfig) -> N8NClient:
    """Sempre devolve um cliente: o n8n e a fonte obrigatoria. Sem chave, o `doctor` e o `/healthz` mostram o 401."""
    env = cfg.active.n8n
    return N8NClient(
        env.url, os.environ.get(env.api_key_env, ""), verify_ssl=env.verify_ssl, timeout=env.timeout_seconds,
    )
