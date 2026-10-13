"""Fuso de exibicao (logs e e-mails): o do time que le, Brasilia."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

FUSO_NOME = "America/Sao_Paulo"
FUSO = ZoneInfo(FUSO_NOME)
ROTULO = "Brasília"


# Função de formatação de hora/data de datetime do instante de leitura 
def formatar_brasilia(dt: datetime | None) -> str:
    """`2026-10-13 16:10:00 (Brasília)`. O instante e o mesmo; muda so a leitura."""
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M:%S") + " (sem fuso)"
    return dt.astimezone(FUSO).strftime("%Y-%m-%d %H:%M:%S") + f" ({ROTULO})"
