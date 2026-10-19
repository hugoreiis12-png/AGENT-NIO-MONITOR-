"""Eventos de um ciclo do Power Automate: falha (por streak), erro tratado e fluxo novo."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...rules.engine import RuleEngine
from ...store.repository import EventRecord, now_utc
from ..base import CollectorResult
from .action_logs import Aprofundamento
from .events import TIPO, evento_de_falha_desktop, evento_erros_tratados


def eventos_de_falha(resultado: CollectorResult, fundo: Aprofundamento, rules: RuleEngine) -> list[EventRecord]:
    """Um evento por fluxo com falha neste ciclo, decidido pelo streak do fluxo."""
    falhas: dict[str, Any] = resultado.extra.get("ultimas_falhas", {})
    eventos = []
    for chave, streak in resultado.extra.get("streaks", {}).items():
        if chave not in falhas:
            continue
        decisao = rules.evaluate(
            event_type=TIPO, streak_count=streak.count, last_notified_at=streak.last_notified_at, now=now_utc(),
        )
        eventos.append(evento_de_falha_desktop(streak, falhas[chave], decisao, fundo.detalhes.get(falhas[chave].id)))
    return eventos


def eventos_de_erro_tratado(
    fundo: Aprofundamento, incidentes: dict[str, Any], decidir: Callable[[EventRecord], EventRecord],
) -> list[EventRecord]:
    eventos = []
    for chave, (sessao, analise) in fundo.tratadas.items():
        evento = evento_erros_tratados(sessao, analise)
        evento.detail["incidente"] = incidentes[chave].isoformat()
        eventos.append(decidir(evento))
    return eventos
