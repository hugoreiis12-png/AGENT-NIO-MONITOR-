"""Lista simbolos publicos definidos no src e nunca referenciados.

Ferramenta de limpeza (nao teste): remove codigo morto sem adivinhar. Rodar,
olhar a saida, apagar o que for realmente orfao.
"""

from __future__ import annotations

import ast
import collections
import pathlib
import sys

RAIZ = pathlib.Path(__file__).resolve().parents[1]
SRC = RAIZ / "src"
TESTS = RAIZ / "tests"


def ler(caminho: pathlib.Path) -> str:
    """utf-8-sig tolera BOM.

    Arquivos criados por `Set-Content -Encoding UTF8` no Windows PowerShell 5.1
    ganham BOM. O `import` do Python engole, mas `ast.parse` nao -- e qualquer
    ferramenta que leia o arquivo direto quebra sem explicar.
    """
    return caminho.read_text(encoding="utf-8-sig")


def definidos(caminho: pathlib.Path) -> set[str]:
    arvore = ast.parse(ler(caminho))
    nomes: set[str] = set()
    for no in arvore.body:
        if isinstance(no, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if not no.name.startswith("_"):
                nomes.add(no.name)
    return nomes


def referenciados(alvos: list[pathlib.Path]) -> collections.Counter[str]:
    usados: collections.Counter[str] = collections.Counter()
    for arquivo in alvos:
        arvore = ast.parse(ler(arquivo))
        for no in ast.walk(arvore):
            if isinstance(no, ast.Name):
                usados[no.id] += 1
            elif isinstance(no, ast.Attribute):
                usados[no.attr] += 1
            elif isinstance(no, ast.ImportFrom):
                usados.update(a.name for a in no.names)
    return usados


def main() -> int:
    src_files = sorted(SRC.rglob("*.py"))
    todos = src_files + sorted(TESTS.rglob("*.py"))
    usados = referenciados(todos)

    com_bom = [
        f"{p.relative_to(RAIZ)}"
        for p in todos
        if p.read_bytes().startswith(b"\xef\xbb\xbf")
    ]
    if com_bom:
        print("ARQUIVOS COM BOM (remova antes de commitar):")
        for b in com_bom:
            print(f"  {b}")

    orfaos: list[str] = []
    for arquivo in src_files:
        for nome in sorted(definidos(arquivo)):
            if usados[nome] == 0:
                orfaos.append(f"{arquivo.relative_to(RAIZ)}::{nome}")

    if orfaos:
        print("\nSIMBOLOS PUBLICOS SEM NENHUMA REFERENCIA:")
        for o in orfaos:
            print(f"  {o}")
    else:
        print("\nnenhum simbolo publico orfao")
    return 0


if __name__ == "__main__":
    sys.exit(main())