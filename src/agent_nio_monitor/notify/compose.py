"""Monta assunto e corpo do alerta: analise do LLM quando houver, texto deterministico senao."""

from __future__ import annotations

from ..llm.analyst import AlertAnalyst, abertura_fallback
from .templates import render


async def compor(analyst: AlertAnalyst, evento, contexto: dict) -> tuple[str, str, str]:  # noqa: ANN001
    """Devolve (assunto, corpo, status_llm)."""
    template = evento.detail.get("template", "generic")
    analise, status = await analyst.analyze(contexto)
    if analise is not None:
        assunto, corpo = render(template, event=evento, contexto={**contexto, "analise": analise.as_dict()})
        return assunto, corpo, status
    abertura = abertura_fallback(contexto)
    assunto, corpo = render(template, event=evento, contexto={**contexto, "abertura": abertura})
    return assunto, f"{abertura}\n{corpo}", status
