"""Redacao de segredos.

Por que este modulo existe: o dump real de `GET /workflows` da instancia contem
credencial de producao em texto puro dentro de `nodes.parameters` --
login SAP (`{"name": "Password", "value": "..."}`) e duas conexoes `dblink` com
`user=... password=...`. Se isso entrar no Postgres de auditoria ou num prompt
de LLM, o banco vira cofre de credencial.

Regra do projeto: o valor ORIGINAL nunca toca disco nem rede. Tudo passa por
`redact()` antes de qualquer escrita ou envio.

Tres estrategias, porque os tres casos reais nao se resolvem com uma so:

1. `_redact_by_key`   -> {"apiKey": "xxx"}            (chave obvious)
2. `_redact_by_label` -> {"name":"Password","value":..} (chave `value` e inofensiva;
                         o segredo e o valor da chave irma `name`)
3. `_redact_text`     -> "... password=postgres ..."   (segredo embutido em texto)

Este modulo nao faz I/O e nao importa nada do resto do sistema de proposito:
ele precisa ser auditavel sozinho.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# 1. Chaves cujo valor e sempre segredo.
# Inclui os termos em PORTUGUES de proposito: o ambiente monitorado usa SAP e
# credenciais nomeadas `senha`, `usuario`, `chave` -- e um redator so，英co,
# deixaria exatamente o caso mais comum deste ambiente vazar.achado por teste.
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "apikey",
        "api_key",
        "accesskey",
        "access_key",
        "secretkey",
        "secret_key",
        "privatekey",
        "private_key",
        "authorization",
        "auth",
        "credentials",
        "credential",
        "connectionstring",
        "connection_string",
        "dsn",
        "passphrase",
        "sessionkey",
        "x-n8n-api-key",
        # --- portugues ---
        "senha",
        "senhas",
        "segredo",
        "chave",
        "chaves",
        "senhaantiga",
        "tokenacesso",
    }
)

# Tokens procurados DENTRO do nome da chave. Cobre `smtpPassword`, `db_password`,
# `userSenha`, `senhaDeAcesso` -- o padrao real em formularios HTTP.
#
# `chave` fica de fora de proposito: e palavrao comum em nao-segredos
# (`chaveDeOrdem`, `chavePrimaria`, `chavePix`) e redigir isso destruiria o
# diagnostico. Fica como match exato (`{"chave": "..."}`) e com `privada`.
_TOKENS_SENHAIS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "apikey",
    "accesstoken",
    "authtoken",
    "senha",
    "segredo",
    "privada",
    "privatekey",
)

# 2. Chaves cujo VALOR nomeia o campo sensivel de um irmao.
_LABEL_KEYS: frozenset[str] = frozenset({"name", "key", "label", "parametername"})

# Substring que, no valor de uma chave de rotulo, caracteriza segredo.
_SENSITIVE_LABEL: re.Pattern[str] = re.compile(
    r"(password|passwd|pwd|secret|token|api[_-]?key|auth|senha|"
    r"private[_-]?key|passphrase)",
    re.IGNORECASE,
)

# 3. Padroes de segredo embutidos em texto livre (SQL, connstring, header).
_TEXT_PATTERNS: tuple[re.Pattern[str], str] = (
    # scheme://user:senha@host  ->  scheme://user:[REDACTED]@host
    (re.compile(r"(?P<scheme>\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)(?P<pw>[^\s@'\"()]+)(?P<at>@)"), r"\g<scheme>" + REDACTED + r"\g<at>"),
    # chave=valor em qualquer string (connstring libpq, dblink, querystring)
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|apikey)\b\s*[=:]\s*"
            r"(?P<q>['\"]?)(?P<v>[^\s;,'\"()]+)(?P=q)"
        ),
        lambda m: f"{m.group(1)}{m.group('q')}{REDACTED}{m.group('q')}",
    ),
    # Bearer <token>  -- TEM que vir antes do padrao de header, senao o header
    # consome so a palavra "Bearer" e deixa o token nu no texto.
    (re.compile(r"(?i)\bBearer\s+(?P<v>[A-Za-z0-9._\-]{8,})"), "Bearer " + REDACTED),
    # Authorization / X-N8N-API-KEY: valor ate o fim da linha
    (
        re.compile(r"(?i)\b(Authorization|X-N8N-API-KEY|X-API-Key)\b\s*:\s*(?P<v>[^\r\n]+)"),
        lambda m: f"{m.group(1)}: {REDACTED}",
    ),
)

# Limite de profundidade. Workflow real tem no max 4-5 niveis. Acima disso e bug
# nosso, e bug nosso prefere falhar alto (redige) a vazar em silencio.
MAX_DEPTH = 24


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(key).lower())


def _is_sensitive_key(key: str) -> bool:
    k = _norm(key)
    if k in _SENSITIVE_KEYS:
        return True
    # pega variantes: "smtpPassword", "db_password", "userSenha", "senhaDeAcesso"
    return any(tok in k for tok in _TOKENS_SENHAIS)


def redact_text(text: str) -> str:
    """Apaga segredos embutidos em texto livre (SQL, connstring, header)."""
    out = text
    for pattern, repl in _TEXT_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _sensitive_siblings(node: dict[str, Any]) -> set[str]:
    """Descobre quais chaves do dict sao segredo por causa de uma chave-irma de rotulo.

    Caso real: n8n HTTP Request guarda body como
        {"name": "Password", "value": "jfc2025"}
    Aqui `value` e secret por causa de `name`. Quem tem o rotulo NAO e o segredo --
    sao os IRMAOS dele que sao.
    """
    flagged: set[str] = set()
    for label_key, label_value in node.items():
        if (
            _norm(label_key) in _LABEL_KEYS
            and isinstance(label_value, str)
            and _SENSITIVE_LABEL.search(label_value)
        ):
            flagged.update(str(k) for k in node if k != label_key)
    return flagged


def _redact(node: Any, depth: int, siblings: frozenset[str]) -> Any:
    if depth > MAX_DEPTH:
        return REDACTED

    if isinstance(node, dict):
        flagged = _sensitive_siblings(node)
        out: dict[str, Any] = {}
        for key, value in node.items():
            ks = str(key)
            if _is_sensitive_key(ks) or ks in siblings or ks in flagged:
                out[ks] = REDACTED if value is not None else None
            else:
                out[ks] = _redact(value, depth + 1, frozenset())
        return out

    if isinstance(node, list):
        return [_redact(item, depth + 1, siblings) for item in node]

    if isinstance(node, tuple):
        return tuple(_redact(item, depth + 1, siblings) for item in node)

    if isinstance(node, str):
        return redact_text(node)

    return node


def redact(payload: Any) -> Any:
    """Ponto de entrada. Idempotente: redact(redact(x)) == redact(x)."""
    return _redact(payload, 0, frozenset())


def contains_secret(value: str) -> bool:
    """Usado por teste para provar que a redacao e total numa string."""
    return redact_text(value) != value