"""Diff estrutural de workflow.

Por que existe em vez de "diff de texto": o que importa para o alerta e
"qual node saiu" e "qual conexao quebrou", nao a mudanca de indentacao.

Importante: a comparacao de conteudo e feita sobre uma **projecao** (ids, nomes,
tipos, chaves de conexao, settings), nao sobre o corpo inteiro. Motivo medido: o
corpo completo tem 11,2 MB em 93 workflows e contem credencial. Diffar o corpo
inteiro seria diffar segredo -- alem de caro.

Se o feature-detect achar `/workflows/{id}/history`, a autoria vem de la.
Se nao achar, `authors` fica None e o email diz isso explicitamente em vez de
omitir (omissao seria parecer que nao houve autor).
"""

from __future__ import annotations

from typing import Any

from ...store.repository import Change

# Campos que NUNCA entram no diff, porque sao ruido ou segredo.
_IGNORED_NODE_KEYS = frozenset({"position", "notes", "color", "notesInFlow"})


def projection(workflow: dict[str, Any]) -> dict[str, Any]:
    """Projecao barata e sensata: muda quando o comportamento muda."""
    nodes = []
    for n in workflow.get("nodes", []):
        nodes.append(
            {
                "id": n.get("id"),
                "name": n.get("name"),
                "type": n.get("type"),
                "typeVersion": n.get("typeVersion"),
                "disabled": bool(n.get("disabled", False)),
                "onError": n.get("onError"),
                "retryOnFail": bool(n.get("retryOnFail", False)),
                # parametros entram so como CHAVE, nunca como VALOR: o valor
                # pode ser senha (e ja vi).
                "param_keys": sorted((n.get("parameters") or {}).keys()),
            }
        )
    nodes.sort(key=lambda n: str(n.get("name") or n.get("id")))

    conexoes: set[tuple[str, str]] = set()
    for origem, saidas in (workflow.get("connections") or {}).items():
        for saida in (saidas or {}).get("main", []) or []:
            for aresta in saida or []:
                destino = aresta.get("node")
                if destino:
                    conexoes.add((str(origem), str(destino)))
    return {
        "active": bool(workflow.get("active", False)),
        "nodes": nodes,
        "connections": sorted(conexoes),
        "settings": workflow.get("settings") or {},
    }


def _node_key(n: dict[str, Any]) -> str:
    return str(n.get("id") or n.get("name"))


def diff_workflows(
    before: dict[str, Any], after: dict[str, Any]
) -> list[Change]:
    """Mudancas estruturais. Retorna [] quando nada relevante mudou."""
    a, b = projection(before), projection(after)
    mudancas: list[Change] = []

    if a.get("active") != b.get("active"):
        mudancas.append(
            Change(
                kind="settings_changed",
                path="active",
                before=a.get("active"),
                after=b.get("active"),
            )
        )

    antes_por_id = {_node_key(n): n for n in a["nodes"]}
    depois_por_id = {_node_key(n): n for n in b["nodes"]}

    for chave in depois_por_id.keys() - antes_por_id.keys():
        mudancas.append(
            Change(kind="node_added", path=chave,
                   after={k: v for k, v in depois_por_id[chave].items() if k not in _IGNORED_NODE_KEYS})
        )
    for chave in antes_por_id.keys() - depois_por_id.keys():
        mudancas.append(
            Change(kind="node_removed", path=chave,
                   before={k: v for k, v in antes_por_id[chave].items() if k not in _IGNORED_NODE_KEYS})
        )
    for chave in antes_por_id.keys() & depois_por_id.keys():
        na, nb = antes_por_id[chave], depois_por_id[chave]
        if na == nb:
            continue
        campos = {k: (na.get(k), nb.get(k)) for k in set(na) | set(nb) if na.get(k) != nb.get(k)}
        # Parametro que so mudou de CHAVE e a mudanca que mais merece alerta:
        # tipico de "adicionaram uma senha hardcoded" ou "tiraram o retry".
        risco = "param_keys" in campos or "type" in campos or "disabled" in campos
        mudancas.append(
            Change(
                kind="node_changed_risky" if risco else "node_changed",
                path=chave,
                before={k: v for k, v in na.items() if k not in _IGNORED_NODE_KEYS},
                after={k: v for k, v in nb.items() if k not in _IGNORED_NODE_KEYS},
            )
        )

    conexoes_a, conexoes_b = set(a["connections"]), set(b["connections"])
    for origem, destino in conexoes_b - conexoes_a:
        mudancas.append(Change(kind="conn_added", path=f"{origem} -> {destino}"))
    for origem, destino in conexoes_a - conexoes_b:
        mudancas.append(Change(kind="conn_removed", path=f"{origem} -> {destino}"))

    if a["settings"] != b["settings"]:
        mudancas.append(
            Change(kind="settings_changed", path="settings",
                   before=a["settings"], after=b["settings"])
        )

    return mudancas


def _rotulo(change: Change) -> str:
    """Nome legivel. O `path` e o id interno do node; quem le o alerta precisa
    do NOME. Um alerta que diz 'node b' e inacaoavel."""
    payload = change.before if isinstance(change.before, dict) else None
    if payload is None and isinstance(change.after, dict):
        payload = change.after
    nome = (payload or {}).get("name")
    if nome and nome != change.path:
        return f"{nome} ({change.path})"
    return change.path


def resumo(mudancas: list[Change]) -> str:
    """Texto curto pro email. Cada linha e um fato, sem adjetivo."""
    if not mudancas:
        return "Nenhuma diferenca estrutural detectada."
    contagem: dict[str, int] = {}
    for m in mudancas:
        contagem[m.kind] = contagem.get(m.kind, 0) + 1
    rotulo = {
        "node_added": "node adicionado",
        "node_removed": "node REMOVIDO",
        "node_changed": "node alterado",
        "node_changed_risky": "node alterado (parametro/tipo)",
        "conn_added": "conexao criada",
        "conn_removed": "conexao REMOVIDA",
        "settings_changed": "setting alterado",
    }
    linhas = ["- " + ", ".join(f"{rotulo.get(k, k)}: {v}" for k, v in sorted(contagem.items()))]
    for m in mudancas:
        if m.kind in ("node_removed", "conn_removed", "node_changed_risky"):
            linhas.append(f"  * {_rotulo(m)}")
    return "\n".join(linhas)


def risco_para_score(mudancas: list[Change]) -> tuple[int, list[str]]:
    """Sinalizacao deterministica. NAO passa pelo LLM (ADR D3)."""
    pontos = 0
    fatores: list[str] = []
    for m in mudancas:
        if m.kind == "conn_removed":
            pontos += 4
            fatores.append(f"conexao removida: {m.path}")
        elif m.kind == "node_removed":
            pontos += 3
            fatores.append(f"node removido: {m.path}")
        elif m.kind == "node_changed_risky":
            pontos += 2
            fatores.append(f"node alterado em campo sensivel: {m.path}")
        elif m.kind == "node_added" or m.kind == "conn_added":
            pontos += 1
        elif m.kind == "settings_changed" and m.path == "active":
            pontos += 2
            fatores.append("ativacao/desativacao")
    return min(10, pontos), fatores


def autoria_de(workflow: dict[str, Any]) -> str | None:
    """`activeVersion.authors` existe nesta instancia. Ausencia e cenario
    conhecido: devolve None e o template diz 'autoria nao disponivel'."""
    version = workflow.get("activeVersion") or {}
    autores = version.get("authors")
    if isinstance(autores, str) and autores.strip():
        return autores.strip()
    return None