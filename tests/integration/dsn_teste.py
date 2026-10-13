"""DSN dos testes de integracao: so `DATABASE_DSN_TEST`, nunca o do agente.

Os testes fazem TRUNCATE. Se compartilhassem `DATABASE_DSN`, cada `pytest`
apagaria os dados do agente em execucao.
"""

from __future__ import annotations

import os

from psycopg.conninfo import conninfo_to_dict


def dsn_de_teste() -> str | None:
    """None quando ausente ou inseguro (igual ao do agente, ou base sem sufixo _test)."""
    dsn = os.environ.get("DATABASE_DSN_TEST")
    if not dsn or dsn == os.environ.get("DATABASE_DSN"):
        return None
    base = conninfo_to_dict(dsn).get("dbname", "")
    return dsn if base.endswith("_test") else None
