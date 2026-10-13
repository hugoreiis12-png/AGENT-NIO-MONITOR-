"""Diff do compose de uma stack: normaliza (sem segredo) e mede quanto mudou."""

from __future__ import annotations

import hashlib
from difflib import SequenceMatcher
from typing import Any

import yaml

from ...redaction import redact_text

HASH_CHARS = 8


def _hash(valor: object) -> str:
    return "h:" + hashlib.sha1(str(valor).encode("utf-8")).hexdigest()[:HASH_CHARS]


def _ocultar_ambiente(ambiente: Any) -> Any:
    """Troca o VALOR de cada variavel por um hash: mudanca de valor continua
    aparecendo no diff, mas o segredo nao e guardado nem enviado."""
    if isinstance(ambiente, dict):
        return {k: _hash(v) for k, v in ambiente.items()}
    if isinstance(ambiente, list):
        saida = []
        for item in ambiente:
            chave, sep, valor = str(item).partition("=")
            saida.append(f"{chave}={_hash(valor)}" if sep else chave)
        return saida
    return ambiente


def normalizar(conteudo: str) -> str:
    """YAML canonico (chaves ordenadas): reordenar nao conta como mudanca."""
    try:
        dados = yaml.safe_load(conteudo)
    except yaml.YAMLError:
        return redact_text(conteudo)
    if not isinstance(dados, dict):
        return redact_text(conteudo)
    for servico in (dados.get("services") or {}).values():
        if isinstance(servico, dict) and "environment" in servico:
            servico["environment"] = _ocultar_ambiente(servico["environment"])
    return yaml.safe_dump(dados, sort_keys=True, allow_unicode=True, default_flow_style=False)


def proporcao_alterada(antes: str, depois: str) -> float:
    """0.0 = identico, 1.0 = nada em comum (por linha do YAML normalizado)."""
    a, b = antes.splitlines(), depois.splitlines()
    if not a and not b:
        return 0.0
    return 1.0 - SequenceMatcher(None, a, b, autojunk=False).ratio()


def _servicos(conteudo: str) -> dict[str, Any] | None:
    try:
        dados = yaml.safe_load(conteudo)
    except yaml.YAMLError:
        return None
    servicos = dados.get("services") if isinstance(dados, dict) else None
    return servicos if isinstance(servicos, dict) else None


def resumo_mudancas(antes: str, depois: str) -> str:
    """Quais servicos entraram, sairam ou mudaram, e em que campos."""
    a, b = _servicos(antes), _servicos(depois)
    if a is None or b is None:
        return "Estrutura do compose nao pode ser comparada por servico."
    partes = []
    if novos := sorted(set(b) - set(a)):
        partes.append(f"servicos adicionados: {', '.join(novos)}")
    if removidos := sorted(set(a) - set(b)):
        partes.append(f"servicos removidos: {', '.join(removidos)}")
    for nome in sorted(set(a) & set(b)):
        campos = sorted(k for k in set(a[nome] or {}) | set(b[nome] or {}) if (a[nome] or {}).get(k) != (b[nome] or {}).get(k))
        if campos:
            partes.append(f"{nome} alterado ({', '.join(campos)})")
    return "; ".join(partes) or "Sem mudanca de servico (ajuste em outras secoes do compose)."
