"""Carrega o `.env` para os testes de integracao.

Sem isso, `DATABASE_DSN_TEST` precisa ser exportado na mao antes de rodar pytest, e
`pytest` simplesmente pula os 28 testes de Postgres sem avisar que faltava
configuracao -- o pior modo de falha: parece que passou.

Regra: variavel de ambiente do sistema tem prioridade sobre o `.env`, igual ao
`config/loader.py`. Isso permite rodar em CI sem arquivo.
"""

from __future__ import annotations

import os
import pathlib

import pytest
from tenacity import wait_none

from agent_nio_monitor.sources.n8n.client import N8NClient
from agent_nio_monitor.sources.portainer.client import PortainerClient
from agent_nio_monitor.sources.powerautomate.client import DataverseClient

RAIZ = pathlib.Path(__file__).resolve().parents[1]
ARQ_ENV = RAIZ / ".env"


def _carrega_dotenv() -> None:
    if not ARQ_ENV.exists():
        return
    for linha in ARQ_ENV.read_text(encoding="utf-8").splitlines():
        limpa = linha.strip()
        if not limpa or limpa.startswith("#") or "=" not in limpa:
            continue
        chave, _, valor = limpa.partition("=")
        os.environ.setdefault(chave.strip(), valor.strip().strip("'\""))


_carrega_dotenv()


@pytest.fixture(autouse=True)
def _retry_sem_espera(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mantem as 3 tentativas, mas sem o backoff real: so o numero de tentativas e contrato."""
    for metodo in (N8NClient._get, PortainerClient._get, DataverseClient.get):
        monkeypatch.setattr(metodo.retry, "wait", wait_none())
