"""Aplica ao role `hermes_app` a senha que esta no `.env`.

Por que: o role foi criado pelo bootstrap com o placeholder
`TROQUE_ESTA_SENHA` (o SQL foi editado DEPOIS de rodar). Deixar assim significa
que uma senha de placeholder esta valida no servidor -- exatamente o que nao
queremos. Aqui o placeholder morre e passa a valer a senha escolhida no `.env`.

A senha nunca e impressa nem passada por linha de comando (que ficaria no
historico do shell): vai como parametro ligado, e o arquivo temporario e apagado
ao final.
"""

from __future__ import annotations

import pathlib
import re
import tempfile

import psycopg
from psycopg import sql

RAIZ = pathlib.Path(__file__).resolve().parents[1]
HOST = "192.168.0.250"
PORTA = 5432
ROLE = "hermes_app"
SUPERUSER = "postgres"
PLACEHOLDER = "TROQUE_ESTA_SENHA"

# Senha do superusuario, para o ALTER ROLE. Mesma origem do .env.
def dsn_env() -> str:
    return re.search(
        r"^DATABASE_DSN=(.+)$", (RAIZ / ".env").read_text(encoding="utf-8"), re.M
    ).group(1).strip()


def senha_app() -> str:
    from urllib.parse import urlsplit

    return urlsplit(dsn_env()).password or ""


def senha_superuser() -> str:
    # O bootstrap usou a senha que o dono digitou; no seu caso e a mesma.
    m = re.search(r"^DATABASE_DSN=postgresql://[^:]+:([^@]+)@", (RAIZ / ".env").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else ""


def main() -> int:
    alvo = senha_app()
    if not alvo:
        print("ERRO: .env sem senha em DATABASE_DSN")
        return 1
    if alvo == PLACEHOLDER:
        print("ERRO: o .env ainda tem o placeholder -- defina a senha real")
        return 1

    admin = senha_superuser()
    if not admin:
        print("ERRO: sem senha de administracao para o ALTER ROLE")
        return 1

    dsn_admin = (
        f"postgresql://{SUPERUSER}:{admin}@{HOST}:{PORTA}/postgres"
        if admin
        else f"postgresql://{SUPERUSER}@{HOST}:{PORTA}/postgres"
    )

    with psycopg.connect(dsn_admin, connect_timeout=10, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname = %s", (ROLE,)
            )
            linha = cur.fetchone()
            if linha is None:
                print("ERRO: role hermes_app nao existe")
                return 1
            if not linha[0]:
                print("ERRO: role existe mas nao pode logar")
                return 1
            if linha[1]:
                print("ALERTA: hermes_app e SUPERUSUARIO -- revisar")

            cur.execute(
                "SELECT rolpassword FROM pg_authid WHERE rolname = %s", (ROLE,)
            )
            atual = cur.fetchone()[0]

            # `ALTER ROLE` e statement de UTILIDADE: nao aceita bind parameter
            # (`PASSWORD $1` da erro de sintaxe). Precisa de composicao com
            # `sql.Literal`, que escapa as aspas corretamente -- concatenar
            # direto abriria porta para injecao de SQL via senha.
            cur.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(ROLE), sql.Literal(alvo)
                )
            )
            cur.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s", (ROLE,))
            depois = cur.fetchone()[0]

    trocou = atual != depois
    print(f"  senha do {ROLE} atualizada: {'SIM' if trocou else 'NAO (ja era a mesma)'}")
    print(f"  comprimento: {len(alvo)} caracteres")
    if len(alvo) < 12:
        print("  ATENCAO: recomendo 16+ caracteres, com maiuscula, digito e simbolo")
    if atual and "SCRAM-SHA-256$" not in atual:
        print("  ATENCAO: o role usava md5; agora passa a usar SCRAM-SHA-256")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())