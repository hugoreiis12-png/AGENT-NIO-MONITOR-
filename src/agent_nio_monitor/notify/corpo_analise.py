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
    confianca = analise.get("confianca")
    titulo_causa = f"Causa apurada (confianca {confianca})" if confianca else "Causa provavel"
    secoes = [_secao("O que aconteceu", analise["resumo"]), _secao(titulo_causa, analise["causa_provavel"])]
    if analise.get("evidencias"):
        secoes.append(_secao("Evidencias", "\n".join(f"- {e}" for e in analise["evidencias"])))
    secoes.append(_secao("Como resolver", numerados))
    detalhes = fatos(contexto, excluir=("stack", "autor"))
    if detalhes:
        secoes.append(_secao("Detalhes", detalhes))
    if url_workflow:
        secoes.append(f"Ver no n8n: {url_workflow}")
    return "\n\n" + "\n\n".join(secoes)
