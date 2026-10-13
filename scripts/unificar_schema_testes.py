"""Unifica o schema das duas suites de integracao em `hermes`.

Por que: existem DOIS databases (`hermes`, `hermes_test`) e UM schema, `hermes`,
nos dois (decisao ADR D10/D14). As migrations tem `CREATE SCHEMA IF NOT EXISTS
hermes` e `SET search_path TO hermes, public` -- o nome e fixo de proposito.

Antes desta correcao as suites discordavam: uma pedia `public`, a outra
`hermes_test`, e `Database(schema=)` so ajusta o `search_path` da conexao -- a
migration ignora. Resultado: `TRUNCATE hermes_test.execution` apontava para uma
tabela que nunca existiu.

Idempotente.
"""

from __future__ import annotations

import pathlib

RAIZ = pathlib.Path(__file__).resolve().parents[1]
ALVOS = [
    "tests/integration/test_postgres_live.py",
    "tests/integration/test_paridade_postgres.py",
]


def main() -> int:
    alterados = 0
    for relativo in ALVOS:
        caminho = RAIZ / relativo
        if not caminho.exists():
            print(f"  AVISO: {relativo} nao existe")
            continue
        original = caminho.read_text(encoding="utf-8")
        novo = original

        # qualificaoes com o schema errado -> sem qualifying (o search_path resolve)
        novo = novo.replace("hermes_test.", "")
        novo = novo.replace("public.execution_probe", "execution_probe")
        # o argumento de schema do Database
        novo = novo.replace('Database(DSN, "hermes_test"', 'Database(DSN, "hermes"')
        novo = novo.replace('Database(DSN, "public"', 'Database(DSN, "hermes"')
        # predicados sobre information_schema / pg_namespace
        novo = novo.replace("table_schema='hermes_test'", "table_schema='hermes'")
        novo = novo.replace("nspname='hermes_test'", "nspname='hermes'")
        # prefixo esperado no nome da particao
        novo = novo.replace(
            'tabela.startswith("hermes_test.execution_")', 'tabela.startswith("execution_")'
        )

        if novo != original:
            caminho.write_text(novo, encoding="utf-8", newline="\n")
            alterados += 1
            print(f"  unificado: {relativo}")
        else:
            print(f"  ja unificado: {relativo}")

    print(f"total: {alterados}/{len(ALVOS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())