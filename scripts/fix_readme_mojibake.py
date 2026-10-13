"""Corrige o mojibake lossy conhecido em README.md.

So o arquivo README.md, linha do titulo da secao. Escrito em Python porque o
padrao `N\xc3O` e indistinguivel de `Não` para um editor de texto -- so a
comparacao por byte revela a diferenca.
"""

from __future__ import annotations

import pathlib

ALVO = pathlib.Path(__file__).resolve().parents[1] / "README.md"

# 'N' + U+00C3 + 'O'  ->  'N' + 'ã' + 'o'
ANTES = "N\u00c3O est\u00e1 verificado"
DEPOIS = "N\u00e3o est\u00e1 verificado"


def main() -> int:
    texto = ALVO.read_text(encoding="utf-8")
    if DEPOIS in texto and ANTES not in texto:
        print("ja corrigido")
        return 0
    if ANTES not in texto:
        print(f"AVISO: padrao nao encontrado em {ALVO}")
        return 1
    ALVO.write_text(texto.replace(ANTES, DEPOIS), encoding="utf-8", newline="\n")
    print("README.md:99 corrigido")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())