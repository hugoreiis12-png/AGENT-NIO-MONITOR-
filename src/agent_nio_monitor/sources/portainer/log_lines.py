"""Linha de log de container: instante, se e erro, assinatura estavel e falha de trigger do n8n.

Calibrado nos logs reais de 19/10/2026 (35 containers): 'WARNING ... Tentativa 1/3 falhou' NAO
e erro (o nivel declarado manda); 'Erro registrado. Origem: X', '[Error: socket hang up]' e
JSON com chave `error` sao.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

NIVEIS_DE_ERRO = frozenset({"error", "err", "critical", "crit", "fatal", "panic", "alert", "emerg"})
NIVEIS_ABAIXO = frozenset({"trace", "debug", "info", "notice", "warn", "warning"})
_NIVEL_DECLARADO = re.compile(
    r"(?:\blevel[=:]\s*\"?|\[|\|\s*|^|\s)(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|CRITICAL|FATAL|PANIC)\b",
    re.IGNORECASE,
)
_PALAVRA_DE_ERRO = re.compile(r"\b(traceback|exception|erro|error|fatal|panic|failed|falhou|falha)\b", re.IGNORECASE)
_TIMESTAMP_DOCKER = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?Z\s?")
_TRIGGER_N8N = re.compile(
    r'The trigger node "(?P<trigger>.+?)" of workflow "(?P<workflow>.+?)" failed with the error: "?(?P<erro>.*?)"?$'
)
_VARIAVEIS = (
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<data>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"\s+"), " "),
)
MAX_ASSINATURA = 200


def separar(linha: str) -> tuple[datetime | None, str]:
    """Linha do Docker com `timestamps=true` -> (instante UTC, texto)."""
    achado = _TIMESTAMP_DOCKER.match(linha)
    if not achado:
        return None, linha.strip()
    instante = datetime.fromisoformat(achado.group(1)).replace(tzinfo=UTC)
    fracao = achado.group(2)
    if fracao:
        instante = instante.replace(microsecond=int(fracao[1:7].ljust(6, "0")))
    return instante, linha[achado.end():].strip()


def eh_erro(texto: str) -> bool:
    if texto.startswith("{"):
        nivel_json = _nivel_json(texto)
        if nivel_json is not None:
            return nivel_json
    declarado = _NIVEL_DECLARADO.search(texto)
    if declarado:
        return declarado.group(1).lower() in NIVEIS_DE_ERRO
    return bool(_PALAVRA_DE_ERRO.search(texto))


def _nivel_json(texto: str) -> bool | None:
    try:
        dados = json.loads(texto)
    except ValueError:
        return None
    if not isinstance(dados, dict):
        return None
    nivel = str(dados.get("level") or dados.get("levelname") or dados.get("severity") or "").lower()
    if nivel:
        return nivel in NIVEIS_DE_ERRO
    return "error" in dados


def normalizar(texto: str) -> str:
    for padrao, troca in _VARIAVEIS:
        texto = padrao.sub(troca, texto)
    return texto.strip().lower()[:MAX_ASSINATURA]


def assinatura(texto: str) -> str:
    """Mesmo erro com numero, data ou id diferente -> mesma assinatura."""
    return hashlib.sha1(normalizar(texto).encode("utf-8")).hexdigest()[:12]


def falha_de_trigger(texto: str) -> tuple[str, str, str] | None:
    """(trigger, workflow, erro) da linha do n8n. Falha de trigger nao gera execucao."""
    achado = _TRIGGER_N8N.search(texto)
    if not achado:
        return None
    return achado.group("trigger"), achado.group("workflow"), achado.group("erro").strip()
