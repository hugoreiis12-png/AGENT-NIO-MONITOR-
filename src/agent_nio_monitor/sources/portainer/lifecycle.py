"""Remocao de container e loop de reinicio. Estado em memoria, como o cache de
`active` dos workflows: a primeira passada so forma a linha de base."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Any

CONFIRMACOES_DE_AUSENCIA = 2


class LifecycleTracker:
    def __init__(self, *, loop_count: int, loop_window: timedelta) -> None:
        self.loop_count = loop_count
        self.loop_window = loop_window
        self._conhecidos: dict[int, dict[str, dict[str, Any]]] = {}
        self._ausencias: dict[tuple[int, str], int] = {}
        self._contagens: dict[str, deque[tuple[datetime, int]]] = {}
        self._ultimo_alerta: dict[str, datetime] = {}

    def removidos(self, endpoint_id: int, atuais: dict[str, dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
        """Containers cujo NOME sumiu. Recriar (compose up) troca o id mas mantem
        o nome, entao nao conta. Exige 2 coletas seguidas sem o nome, para nao
        confundir com a janela entre `rm` e `create`."""
        conhecidos = self._conhecidos.get(endpoint_id)
        if conhecidos is None:
            self._conhecidos[endpoint_id] = dict(atuais)
            return []
        confirmados = []
        for nome, info in list(conhecidos.items()):
            if nome in atuais:
                self._ausencias.pop((endpoint_id, nome), None)
                continue
            vezes = self._ausencias.get((endpoint_id, nome), 0) + 1
            self._ausencias[(endpoint_id, nome)] = vezes
            if vezes >= CONFIRMACOES_DE_AUSENCIA:
                confirmados.append((nome, info))
                del conhecidos[nome]
                del self._ausencias[(endpoint_id, nome)]
        conhecidos.update(atuais)
        return confirmados

    def reinicios_na_janela(self, container_id: str, restart_count: int, agora: datetime) -> int | None:
        """Quantos reinicios na janela, se for loop (>= limite) e ainda nao alertado
        nesta janela; None caso contrario."""
        fila = self._contagens.setdefault(container_id, deque())
        fila.append((agora, restart_count))
        while fila and agora - fila[0][0] > self.loop_window:
            fila.popleft()
        reinicios = restart_count - fila[0][1]
        if reinicios < self.loop_count:
            return None
        ultimo = self._ultimo_alerta.get(container_id)
        if ultimo is not None and agora - ultimo <= self.loop_window:
            return None
        self._ultimo_alerta[container_id] = agora
        return reinicios
