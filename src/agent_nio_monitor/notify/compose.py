"""Monta a mensagem do alerta: analise do LLM quando houver, texto deterministico senao.

O texto simples continua sendo gerado sempre (alternativa do e-mail, registro no
banco e canal de log); o HTML com a arte e a versao principal.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config.schema import AppConfig
from ..llm.analyst import AlertAnalyst, abertura_fallback
from .html_render import render_html
from .links import link_do_evento
from .templates import ACAO_PADRAO, render


@dataclass(frozen=True)
class Mensagem:
    assunto: str
    corpo: str
    html: str
    status_llm: str


async def compor(analyst: AlertAnalyst, evento, contexto: dict, cfg: AppConfig) -> Mensagem:  # noqa: ANN001
    template = evento.detail.get("template", "generic")
    analise, status = await analyst.analyze(contexto)
    abertura = abertura_fallback(contexto)
    texto_ctx = {**contexto, "url_workflow": _url_n8n(evento, cfg)}
    if analise is not None:
        assunto, corpo = render(template, event=evento, contexto={**texto_ctx, "analise": analise.as_dict()})
    else:
        assunto, corpo = render(template, event=evento, contexto={**texto_ctx, "abertura": abertura})
        corpo = f"{abertura}\n{corpo}"
    html = render_html(
        evento=evento, contexto=contexto, analise=analise.as_dict() if analise else None, abertura=abertura,
        acao_padrao=ACAO_PADRAO.get(template, ACAO_PADRAO["generic"]), assunto=assunto, cfg=cfg,
    )
    return Mensagem(assunto, corpo, html, status)


def _url_n8n(evento, cfg: AppConfig) -> str:  # noqa: ANN001
    """O texto so imprime 'Ver no n8n:' -- entao so recebe URL de workflow."""
    link = link_do_evento(evento.detail, cfg)
    return link[1] if link and link[0] == "Abrir no n8n" else ""
