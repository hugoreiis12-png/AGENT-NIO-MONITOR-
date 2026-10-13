"""Valida o SQL de criacao de banco com o parser do PostgreSQL.

`migrations/00_criar_banco.sql` mistura SQL com meta-comandos do psql
(`\\set`, `\\connect`, `\\gexec`), que nao sao grammar do Postgres. Este script
remove os meta-comandos de forma declarativa -- sem editar o arquivo de
producao -- e confirma que o que sobra e SQL valido.

Nao substitui executar no servidor: prova sintaxe, nao permissao.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from pglast import parse_sql

RAIZ = Path(__file__).resolve().parents[1]
ARQUIVO = RAIZ / "sql" / "00_criar_banco.sql"


def remove_meta_comandos(sql: str) -> str:
    """Tira o que e do psql, nao do Postgres.

    `\\gexec` vira PONTO E VIRGULA, nao nada: o `SELECT ... WHERE NOT EXISTS
    (...)` precisa terminar em `;` senao o parser junta com o statement seguinte
    e acusa "near SELECT".
    """
    sql = re.sub(r"\\gexec", ";", sql)
    sql = re.sub(r"^\s*\\[a-z]+\b.*$", "", sql, flags=re.M | re.I)
    sql = re.sub(r":\w+", "1", sql)
    return sql


def main() -> int:
    original = ARQUIVO.read_text(encoding="utf-8")
    limpo = remove_meta_comandos(original)
    try:
        statements = parse_sql(limpo)
    except Exception as exc:  # noqa: BLE001 - relatar o erro de sintaxe e o objetivo
        print(f"ERRO DE SINTAXE em {ARQUIVO.name}: {exc}")
        return 1

    print(f"{ARQUIVO.name}: {len(statements)} statements SQL validos")

    # Guarda critica: este arquivo NAO pode estar em `migrations/`.
    # `Database.migrate()` le migrations/*.sql no boot do agente; se o bootstrap
    # estivesse la, o processo tentaria CREATE ROLE e CREATE DATABASE sozinho,
    # sem revisao humana.
    migracoes = RAIZ / "migrations"
    if ARQUIVO.parent == migracoes:
        print("ERRO: bootstrap nao pode ficar em migrations/ -- seria aplicado no boot")
        return 1
    for outro in migracoes.glob("*.sql"):
        corpo = outro.read_text(encoding="utf-8")
        for perigoso in ("CREATE DATABASE", "CREATE ROLE", "ALTER ROLE"):
            if perigoso in corpo.upper():
                print(f"ERRO: {outro.name} contem '{perigoso}' -- operacao de infra")
                return 1
    print("  migrations/ contem apenas DDL de schema (sem CREATE DATABASE/ROLE)")

    # O SQL nao pode embutir senha real. Heuristica: procura um
    # `PASSWORD 'valor'` NAO comentado. A secao de seguranca documenta um
    # `ALTER ROLE ... PASSWORD '<senha-forte>'` comentado, e isso nao e vazamento.
    vazamentos = []
    for linha in original.splitlines():
        s = linha.strip()
        if s.startswith("--"):
            continue
        m = re.search(r"PASSWORD\s+'([^']*)'", s, re.I)
        if m and m.group(1) not in ("", "<senha-forte>", "<senha>"):
            vazamentos.append(s)
    if vazamentos:
        print(f"  FALHA: senha embutida em {len(vazamentos)} linha(s) nao comentada(s)")
        for v in vazamentos:
            print(f"    {v[:70]}")
        return 1
    print("  senha: nenhuma senha embutida em statement executavel")

    # Guardas que o arquivo precisa ter.
    for exigido in ("NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE",
                    "hermes_app", "hermes_test"):
        if exigido not in original:
            print(f"  FALTA: {exigido}")
            return 1
    print("  guardas de menor privilegio presentes")
    return 0


if __name__ == "__main__":
    sys.exit(main())