"""Manutencao periodica das particoes: cria os meses a frente e, se configurado, aplica a retencao.

So no boot nao bastava: `ensure_partitions(2)` cobre ate o mes seguinte, e um agente que
nunca reinicia passava a falhar todo INSERT no primeiro dia sem particao.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

MESES_A_FRENTE = 2


class BancoParticionado(Protocol):
    def ensure_partitions(self, months_ahead: int = MESES_A_FRENTE) -> None: ...

    def drop_partitions_older_than(self, days: int) -> list[tuple[str, str]]: ...


class PartitionMaintenance:
    def __init__(self, db: BancoParticionado, retention_days: int | None) -> None:
        self.db, self.retention_days = db, retention_days

    async def run(self) -> int:
        """Devolve quantas particoes foram removidas. `to_thread`: o psycopg e sincrono."""
        await asyncio.to_thread(self.db.ensure_partitions, MESES_A_FRENTE)
        if self.retention_days is None:
            return 0
        return len(await asyncio.to_thread(self.db.drop_partitions_older_than, self.retention_days))
