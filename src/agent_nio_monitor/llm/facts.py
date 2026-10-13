"""Fatos do alerta: o MESMO texto alimenta o prompt do LLM e a secao "Detalhes" do e-mail."""

from __future__ import annotations

CAMPOS_DE_FATO = (
    ("workflow_nome", "Recurso"), ("recurso", "Recurso"), ("stack", "Stack"),
    ("ocorrencias", "Ocorrencias"), ("primeira", "Primeira falha"), ("ultima", "Ultima falha"),
    ("no", "No que falhou"), ("erro", "Erro"), ("detalhe", "Detalhe"),
    ("resumo_diff", "Mudancas"), ("autor", "Ultima alteracao por"), ("logs", "Log recente"),
)
MAX_FATO = 1200


def fatos(contexto: dict, excluir: tuple[str, ...] = ()) -> str:
    linhas = []
    for chave, rotulo in CAMPOS_DE_FATO:
        valor = contexto.get(chave)
        linha = f"{rotulo}: {str(valor)[:MAX_FATO]}"
        if chave not in excluir and valor not in (None, "") and linha not in linhas:
            linhas.append(linha)
    return "\n".join(linhas)
