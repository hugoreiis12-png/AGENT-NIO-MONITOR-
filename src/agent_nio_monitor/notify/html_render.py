"""E-mail HTML com a arte JFC: um template por faixa de severidade.

Vermelho (CRITICO, ALTO), amarelo (MEDIO), verde (BAIXO, INFO). Todo texto vem de
erro, log ou nome de recurso -- e portanto nao confiavel --, entao o Jinja roda com
autoescape. O unico link e o do botao, montado a partir da configuracao.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..config.schema import AppConfig
from ..fuso import formatar_brasilia
from ..llm.facts import pares
from .links import link_do_evento

TEMPLATES_DIR = Path(__file__).parent / "html"
PREHEADER_MAX = 110
SEM_DETALHE = ("stack", "autor", "logs")



@dataclass(frozen=True)
class Tema:
    arquivo: str
    titulo: str
    hero: str
    cor: str
    cor_texto: str


TEMAS = {
    "critico": Tema("alerta_critico.html.j2", "Hora de agir!", "hero_critico.jpg", "#C00917", "#C00917"),
    "medio": Tema("alerta_medio.html.j2", "Vamos com calma!", "hero_medio.jpg", "#F47F12", "#C25E00"),
    "baixo": Tema("alerta_baixo.html.j2", "Tudo certo!", "hero_baixo.jpg", "#037328", "#037328"),
}
TEMA_POR_SEVERIDADE = {"critical": "critico", "high": "critico", "medium": "medio", "low": "baixo", "info": "baixo"}
ROTULO_POR_SEVERIDADE = {"critical": "CRÍTICO", "high": "ALTO", "medium": "MÉDIO", "low": "BAIXO", "info": "INFO"}

_ambiente = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True, undefined=StrictUndefined,
    trim_blocks=True, lstrip_blocks=True,
)


def tema_da_severidade(severidade: str) -> Tema:
    return TEMAS[TEMA_POR_SEVERIDADE.get(severidade, "medio")]


def _quem_mexeu(contexto: dict) -> list[tuple[str, str]]:
    campos = (
        ("stack", "Stack"), ("responsavel_stack", "Responsável pela stack"),
        ("responsavel_fluxo", "Responsável pelo fluxo"), ("autor", "Última alteração por"),
    )
    return [(rotulo, str(contexto[chave])) for chave, rotulo in campos if contexto.get(chave)]


def render_html(
    *, evento: Any, contexto: dict, analise: dict | None, abertura: str, acao_padrao: str, assunto: str, cfg: AppConfig,
) -> str:
    """`analise` (resumo/causa/passos do LLM) quando houver; senao `abertura`
    deterministica e a acao padrao do template. Nunca levanta por dado ausente."""
    tema = tema_da_severidade(evento.severity)
    resumo = analise["resumo"] if analise else abertura
    passos = (analise.get("como_resolver") if analise else None) or [acao_padrao]
    link = link_do_evento(evento.detail, cfg)
    return _ambiente.get_template(tema.arquivo).render(
        assunto=assunto, preheader=resumo[:PREHEADER_MAX], tema=tema,
        rotulo=ROTULO_POR_SEVERIDADE.get(evento.severity, evento.severity.upper()),
        subtitulo=evento.title, assets=cfg.mail.assets_base_url.rstrip("/"), suporte=cfg.mail.support_email,
        resumo=resumo, causa=analise["causa_provavel"] if analise else None, passos=passos,
        confianca=(analise or {}).get("confianca"), evidencias=(analise or {}).get("evidencias") or [],
        detalhes=pares(contexto, excluir=SEM_DETALHE), log=contexto.get("logs"),
        quem=_quem_mexeu(contexto), quando=formatar_brasilia(evento.ts),
        botao={"texto": link[0], "url": link[1]} if link else None,
    )
