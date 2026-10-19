"""Fatos do alerta: os MESMOS dados alimentam o prompt do LLM e a secao "Detalhes" do e-mail."""

from __future__ import annotations

CAMPOS_DE_FATO = (
    ("workflow_nome", "Recurso"), ("recurso", "Recurso"), ("stack", "Stack"),
    ("ocorrencias", "Ocorrencias"), ("primeira", "Primeira falha"), ("ultima", "Ultima falha"),
    ("no", "No que falhou"), ("erro", "Erro"), ("detalhe", "Detalhe"), ("caminho", "Caminho executado"),
    ("resumo_diff", "Mudancas"), ("autor", "Ultima alteracao por"), ("logs", "Log recente"),
)
MAX_FATO = 1200


def pares(contexto: dict, excluir: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """(rotulo, valor) dos fatos presentes, sem repetir o mesmo par."""
    saida: list[tuple[str, str]] = []
    for chave, rotulo in CAMPOS_DE_FATO:
        valor = contexto.get(chave)
        if chave in excluir or valor in (None, ""):
            continue
        par = (rotulo, str(valor)[:MAX_FATO])
        if par not in saida:
            saida.append(par)
    return saida


def fatos(contexto: dict, excluir: tuple[str, ...] = ()) -> str:
    return "\n".join(f"{rotulo}: {valor}" for rotulo, valor in pares(contexto, excluir))
