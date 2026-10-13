"""Diagnostico de autenticacao do hermes_app -- sem exibir senha.

Testa, em ordem, sem imprimir o valor:
  1. a senha que esta no `.env`
  2. o placeholder original do bootstrap (`TROQUE_ESTA_SENHA`)
  3. senha vazia

Cada tentativa e registrada por SIM/NAO. Serve para descobrir se o `.env` esta
com outra senha, se o role foi criado com o placeholder, ou se ha problema de
autenticacao no servidor (pg_hba).
"""

from __future__ import annotations

import pathlib
import re

import psycopg

RAIZ = pathlib.Path(__file__).resolve().parents[1]
HOST = "192.168.0.250"
PORTA = 5432
ROLE = "hermes_app"
DB = "hermes_test"
PLACEHOLDER = "TROQUE_ESTA_SENHA"


def senha_do_env() -> str | None:
    if not (RAIZ / ".env").exists():
        return None
    m = re.search(
        r"^DATABASE_DSN=postgresql://[^:]+:([^@]+)@", (RAIZ / ".env").read_text(encoding="utf-8"), re.M
    )
    return m.group(1) if m else None


def testar(rotulo: str, senha: str | None) -> bool:
    dsn = f"postgresql://{ROLE}:{senha}@{HOST}:{PORTA}/{DB}" if senha is not None else (
        f"postgresql://{ROLE}@{HOST}:{PORTA}/{DB}"
    )
    try:
        with psycopg.connect(dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_user, current_database()")
                u, d = cur.fetchone()
            print(f"  {rotulo:28} OK   (user={u}, db={d})")
            return True
    except psycopg.OperationalError as exc:
        msg = str(exc).strip().splitlines()[0][:60]
        print(f"  {rotulo:28} FALHOU  {msg}")
        return False


def main() -> int:
    print("tentativas de autenticacao:")
    do_env = senha_do_env()
    if do_env is None:
        print("  .env sem DATABASE_DSN -- nada a testar")
        return 1
    if testar("senha do .env", do_env):
        print("\n  A senha do .env funciona. Nada a corrigir.")
        return 0
    if do_env != PLACEHOLDER and testar("placeholder do bootstrap", PLACEHOLDER):
        print(
            "\n  ACHADO: o role foi criado com o PLACEHOLDER, nao com a senha\n"
            "  que esta no .env. Voce editou o SQL depois de rodar.\n"
            "  Faca: ALTER ROLE hermes_app PASSWORD '<a senha do .env>';"
        )
        return 2
    if testar("senha vazia", None):
        print("\n  ACHADO: o role aceita senha vazia -- risco de seguranca.")
        return 3
    print(
        "\n  Nenhuma das senhas testadas funcionou. Causas provaveis:\n"
        "   - pg_hba.conf exige ssl ou outro metodo para 192.168.0.134\n"
        "   - a senha real nunca foi aplicada no role"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())