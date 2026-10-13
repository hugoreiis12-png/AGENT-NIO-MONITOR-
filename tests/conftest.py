"""Carrega o `.env` para os testes de integracao.

Sem isso, `DATABASE_DSN` precisa ser exportado na mao antes de rodar pytest, e
`pytest` simplesmente pula os 28 testes de Postgres sem avisar que faltava
configuracao -- o pior modo de falha: parece que passou.

Regra: variavel de ambiente do sistema tem prioridade sobre o `.env`, igual ao
`config/loader.py`. Isso permite rodar em CI sem arquivo.
"""

from __future__ import annotations

import os
import pathlib

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