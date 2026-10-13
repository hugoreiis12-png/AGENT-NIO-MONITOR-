"""Calculos puros sobre o que o Docker devolve: sem rede, sem estado."""

from __future__ import annotations

import re
from typing import Any

UPTIME_RECENTE = re.compile(r"^Up (Less than a second|\d+ seconds?|About a minute|[12] minutes?)\b")


def eh_candidato_a_loop(row: dict[str, Any]) -> bool:
    """Reiniciando agora, ou de pe ha menos de ~3 min: so esses justificam um
    `inspect` (RestartCount nao vem na listagem)."""
    if (row.get("State") or "").lower() == "restarting":
        return True
    status = row.get("Status") or ""
    return status.startswith("Restarting") or bool(UPTIME_RECENTE.match(status))


def totais_de_cpu(stats: dict[str, Any]) -> tuple[int, int] | None:
    """(uso do container, uso total do host), ambos acumulados em ns."""
    cpu = stats.get("cpu_stats") or {}
    uso = (cpu.get("cpu_usage") or {}).get("total_usage")
    sistema = cpu.get("system_cpu_usage")
    if uso is None or sistema is None:
        return None
    return int(uso), int(sistema)


def cpu_do_host_pct(anterior: tuple[int, int], atual: tuple[int, int]) -> float | None:
    """Fatia do CPU TOTAL do host usada entre duas coletas (media do intervalo)."""
    delta_uso, delta_sistema = atual[0] - anterior[0], atual[1] - anterior[1]
    if delta_sistema <= 0 or delta_uso < 0:
        return None
    return round(delta_uso / delta_sistema * 100, 2)


def memoria_pct(stats: dict[str, Any]) -> float | None:
    """Uso real / limite. Desconta cache de arquivo, que o kernel devolve sob pressao."""
    mem = stats.get("memory_stats") or {}
    uso, limite = mem.get("usage"), mem.get("limit")
    if not uso or not limite:
        return None
    detalhe = mem.get("stats") or {}
    cache = detalhe.get("inactive_file", detalhe.get("cache", 0))
    return round(max(int(uso) - int(cache), 0) / int(limite) * 100, 2)
