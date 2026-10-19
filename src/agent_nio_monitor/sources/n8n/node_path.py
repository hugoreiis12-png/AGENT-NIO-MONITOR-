"""Leitura no a no de uma execucao do n8n: o que rodou, com quantos itens, o que errou e por que algo nao rodou.

So funcoes puras sobre o JSON da API (`workflowData` + `data.resultData.runData`). Formato
verificado na instancia real em 19/10/2026: cada entrada de `runData[no]` traz `startTime`,
`executionTime`, `source` e `data.main` (uma lista de itens por saida).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Nos cujo trabalho e ESCOLHER a saida: saida vazia neles e decisao, nao defeito.
TIPOS_QUE_DESVIAM = frozenset({"if", "switch", "filter", "compareDatasets", "removeDuplicates"})
CONTINUA_EM_ERRO = frozenset({"continueRegularOutput", "continueErrorOutput"})
MAX_PASSOS_NO_RESUMO = 25
MAX_PROFUNDIDADE = 15
MAX_ERRO = 300


@dataclass(frozen=True)
class Passo:
    no: str
    inicio: int
    itens: int
    duracao_ms: int
    erro: str | None


def run_data(detalhe: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return ((detalhe.get("data") or {}).get("resultData") or {}).get("runData") or {}


def nos_ativos(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Nome -> definicao dos nos que podem rodar (sem desativados nem notas)."""
    return {
        n["name"]: n for n in workflow.get("nodes", [])
        if not n.get("disabled") and not str(n.get("type", "")).endswith("stickyNote")
    }


def _tipo(no: dict[str, Any] | None) -> str:
    return str((no or {}).get("type", "")).rsplit(".", 1)[-1]


def _saidas(execucao: dict[str, Any]) -> list[list[dict[str, Any]]]:
    return [saida or [] for saida in ((execucao.get("data") or {}).get("main") or [])]


def itens_na_saida(execucoes: list[dict[str, Any]], indice: int) -> int:
    return sum(len(s[indice]) for s in map(_saidas, execucoes) if indice < len(s))


def _mensagem(erro: Any) -> str:
    if isinstance(erro, dict):
        partes = [str(erro[k]).strip() for k in ("message", "description") if erro.get(k)]
        return " | ".join(dict.fromkeys(partes))[:MAX_ERRO] or "erro sem mensagem"
    return str(erro)[:MAX_ERRO]


def caminho(runs: dict[str, list[dict[str, Any]]]) -> list[Passo]:
    """Nos que rodaram, na ordem em que comecaram."""
    passos = []
    for no, execucoes in runs.items():
        erro = next((e["error"] for e in execucoes if e.get("error")), None)
        passos.append(Passo(
            no=no, inicio=min(int(e.get("startTime") or 0) for e in execucoes),
            itens=sum(len(item) for e in execucoes for item in _saidas(e)),
            duracao_ms=sum(int(e.get("executionTime") or 0) for e in execucoes),
            erro=_mensagem(erro) if erro else None,
        ))
    return sorted(passos, key=lambda p: p.inicio)


def resumo_do_caminho(runs: dict[str, list[dict[str, Any]]]) -> str | None:
    """Texto do caminho para o alerta: '1. Webhook (1 item, 3 ms)'."""
    passos = caminho(runs)
    if not passos:
        return None
    linhas = [
        f"{i}. {p.no} ({p.itens} item(ns), {p.duracao_ms} ms)" + (f" -- ERRO: {p.erro}" if p.erro else "")
        for i, p in enumerate(passos[:MAX_PASSOS_NO_RESUMO], 1)
    ]
    if len(passos) > MAX_PASSOS_NO_RESUMO:
        linhas.append(f"... mais {len(passos) - MAX_PASSOS_NO_RESUMO} no(s)")
    return "\n".join(linhas)


def erros_escondidos(workflow: dict[str, Any], runs: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    """No -> erro, em execucao que terminou com SUCESSO.

    Tres formas, todas vistas como 'verde' na lista de execucoes: erro gravado na
    entrada do no; `continueErrorOutput` mandando itens para a ultima saida (a de erro);
    `continueRegularOutput`/`continueOnFail` com `json.error` no item.
    """
    nos = nos_ativos(workflow)
    achados: dict[str, str] = {}
    for nome, execucoes in runs.items():
        if erro := next((e["error"] for e in execucoes if e.get("error")), None):
            achados[nome] = _mensagem(erro)
        elif erro_em_item := _erro_em_itens(nos.get(nome), execucoes):
            achados[nome] = erro_em_item
    return achados


def _erro_em_itens(no: dict[str, Any] | None, execucoes: list[dict[str, Any]]) -> str | None:
    if not no or (no.get("onError") not in CONTINUA_EM_ERRO and not no.get("continueOnFail")):
        return None
    for saidas in map(_saidas, execucoes):
        candidatas = saidas[-1:] if no.get("onError") == "continueErrorOutput" else saidas
        for item in (i for s in candidatas for i in s):
            erro = (item.get("json") or {}).get("error")
            if erro:
                return _mensagem(erro)
    return None


def pais(workflow: dict[str, Any], alvo: str) -> list[tuple[str, int]]:
    """(no de origem, indice da saida) de toda conexao `main` que chega em `alvo`."""
    return [
        (origem, indice)
        for origem, conexoes in (workflow.get("connections") or {}).items()
        for indice, destinos in enumerate((conexoes or {}).get("main") or [])
        for destino in destinos or []
        if destino.get("node") == alvo
    ]


def por_que_nao_rodou(workflow: dict[str, Any], runs: dict[str, list[dict[str, Any]]], alvo: str) -> str:
    """Sobe o grafo ate o primeiro no que RODOU e diz o que ele fez com a saida que levava a `alvo`."""
    nos = nos_ativos(workflow)
    fila, vistos = [alvo], {alvo}
    for _ in range(MAX_PROFUNDIDADE):
        if not fila:
            break
        atual = fila.pop(0)
        for origem, indice in pais(workflow, atual):
            if origem in runs:
                return _explica(origem, nos.get(origem), indice, runs[origem], atual)
            if origem not in vistos:
                vistos.add(origem)
                fila.append(origem)
    return "nenhum no anterior rodou nesta execucao (o gatilho dela foi outro)"


def _explica(origem: str, no: dict[str, Any] | None, indice: int, execucoes: list[dict], seguinte: str) -> str:
    itens = itens_na_saida(execucoes, indice)
    if itens > 0:
        return f"'{origem}' entregou {itens} item(ns) para '{seguinte}', que mesmo assim nao rodou (execucao interrompida?)"
    if _tipo(no) in TIPOS_QUE_DESVIAM:
        return f"'{origem}' mandou os itens por outra saida (saida {indice} vazia)"
    return f"'{origem}' devolveu 0 itens na saida {indice}, entao '{seguinte}' nao tinha o que processar"
