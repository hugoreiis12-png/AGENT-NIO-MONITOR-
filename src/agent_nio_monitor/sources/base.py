"""Contrato comum dos coletores."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


def _erro_total_de_endpoints(escuros: list[int], total: int) -> str | None:
    """`error` so quando NENHUM endpoint respondeu -- total darkness.

    Distingue os dois casos que antes eram o mesmo "saudavel": parte das
    fontes caiu (`failures > 0`, visivel no /healthz) e nenhuma fonte
    responde (`error`, 503). Um monitor que so conhece "ok ou nao" trata
    subtotal como total, e e exatamente o buraco que esconde um endpoint
    inteiro fora do ar.
    """
    if not escuros or len(escuros) < total:
        return None
    return f"nenhum endpoint respondeu (illegiveis: {escuros})"


@dataclass
class CollectorResult:
    collected: int = 0
    failures: int = 0
    gap: bool = False
    duration_ms: int = 0
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None


def dentro_de_quiet_hours(
    inicio: str, fim: str, agora: datetime
) -> bool:
    """Janela que atravessa meia-noite (ex.: 22:00 -> 08:00) e o caso real
    configurado. Erro classico aqui e notificar todo mundo as 3h da manha."""
    hi, mi = (int(p) for p in inicio.split(":"))
    fi, fmi = (int(p) for p in fim.split(":"))
    agora_min = agora.hour * 60 + agora.minute
    inicio_min, fim_min = hi * 60 + mi, fi * 60 + fmi
    if inicio_min <= fim_min:
        return inicio_min <= agora_min < fim_min
    return agora_min >= inicio_min or agora_min < fim_min

def instante(valor: str | int | float | None) -> datetime | None:
    """ISO (n8n) ou epoch em segundos (Portainer) -> datetime UTC."""
    if valor in (None, ""):
        return None
    if isinstance(valor, (int, float)):
        return datetime.fromtimestamp(valor, UTC)
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def ainda_em_edicao(quando: datetime | None, minutos: int, agora: datetime) -> bool:
    """Alterado ha menos de `minutos`: espera a edicao terminar e alerta UMA vez com tudo.

    Medido em 19/10/2026: o mesmo workflow gerou 7 e-mails de "foi alterado" em 4 h -- um
    por save no editor."""
    return minutos > 0 and quando is not None and agora - quando < timedelta(minutes=minutos)
