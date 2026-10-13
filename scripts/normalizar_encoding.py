"""Remove BOM e normaliza fim-de-linha para LF.

Por que: `Set-Content -Encoding UTF8` no Windows PowerShell 5.1 grava BOM (U+FEFF)
e CRLF. O `import` do Python engole o BOM, entao o problema fica invisivel --
mas `ast.parse`, `pglast`, `grep -P` e varios linters quebram sem explicar.
18 arquivos do projeto tinham BOM quando este script foi escrito.

Idempotente.
"""

from __future__ import annotations

import pathlib
import sys

RAIZ = pathlib.Path(__file__).resolve().parents[1]
EXTENSOES = {".py", ".md", ".yaml", ".yml", ".sql", ".toml"}
IGNORAR = {".venv", "__pycache__", ".git", "egg-info", ".ruff_cache", ".pytest_cache"}
BOM = b"\xef\xbb\xbf"


def main() -> int:
    removidos = 0
    normalizados = 0
    for caminho in sorted(RAIZ.rglob("*")):
        if not caminho.is_file() or caminho.suffix not in EXTENSOES:
            continue
        if any(parte in IGNORAR for parte in caminho.parts):
            continue
        bruto = caminho.read_bytes()
        novo = bruto
        if novo.startswith(BOM):
            novo = novo[len(BOM) :]
            removidos += 1
        if b"\r\n" in novo:
            novo = novo.replace(b"\r\n", b"\n")
            normalizados += 1
        if novo != bruto:
            caminho.write_bytes(novo)

    print(f"BOM removidos: {removidos}")
    print(f"CRLF -> LF: {normalizados}")
    return 0


if __name__ == "__main__":
    sys.exit(main())