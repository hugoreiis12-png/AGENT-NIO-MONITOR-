"""Corpo do e-mail quando o LLM analisou o alerta: enxuto, na ordem que se le."""

from __future__ import annotations

from ..llm.facts import fatos

SECAO = "{titulo}\n{traco}\n{texto}"


def _secao(titulo: str, texto: str) -> str:
    return f"{titulo}\n{'-' * len(titulo)}\n{texto}"


def corpo_com_analise(contexto: dict, acao_padrao: str, url_workflow: str = "") -> str:
    analise = contexto["analise"]
    passos = analise.get("como_resolver") or [acao_padrao]
    numerados = "\n".join(f"{i}. {p}" for i, p in enumerate(passos, 1))
    secoes = [
        _secao("O que aconteceu", analise["resumo"]),
        _secao("Causa provavel", analise["causa_provavel"]),
        _secao("Como resolver", numerados),
    ]
    detalhes = fatos(contexto, excluir=("stack", "autor"))
    if detalhes:
        secoes.append(_secao("Detalhes", detalhes))
    if url_workflow:
        secoes.append(f"Ver no n8n: {url_workflow}")
    return "\n\n" + "\n\n".join(secoes)
