"""Move a senha do role para o `.env` e limpa o SQL versionado.

Por que: `sql/00_criar_banco.sql` e versionavel e continha a senha real do
`hermes_app`. `.gitignore` cobre `.env` mas nao `sql/` -- no primeiro `git add .`
a senha entraria no historico, e historico e imutavel.

Este script:
  1. extrai a senha do SQL
  2. grava o DSN real no `.env` (gitignored)
  3. remove a senha do SQL, deixando o comando `ALTER ROLE ... PASSWORD` como
     comentario para execucao manual -- o arquivo versionavel fica seguro por
     construcao, nao por disciplina
  4. nunca imprime a senha

Idempotente.
"""

from __future__ import annotations

import pathlib
import re
import sys

RAIZ = pathlib.Path(__file__).resolve().parents[1]
SQL = RAIZ / "sql" / "00_criar_banco.sql"
ENV = RAIZ / ".env"

HOST = "192.168.0.250"
PORTA = "5432"
ROLE = "hermes_app"
SENHA_PLACEHOLDER = "TROQUE_ESTA_SENHA"


def extrair_senha(sql: str) -> str | None:
    m = re.search(r"CREATE ROLE\s+\w+\s+LOGIN\s+PASSWORD\s+'([^']*)'", sql, re.I)
    return m.group(1) if m else None


def limpar_sql(sql: str) -> str:
    """Troca a senha embutida por um comando manual comentado."""
    if SENHA_PLACEHOLDER in sql:
        return sql  # ja esta limpo
    sql = re.sub(
        r"CREATE ROLE(\s+\w+)(\s+)LOGIN(\s+)PASSWORD\s+'[^']*';",
        r"CREATE ROLE\1\2LOGIN;",
        sql,
        flags=re.I,
    )
    aviso = f"""
-- NOTA DE SEGURANCA
-- A senha do role NAO fica neste arquivo. Este arquivo e versionavel.
-- Depois de criar o role, defina a senha na mao, fora do versionado:
--
--   \\psql -h {HOST} -p {PORTA} -U postgres -d postgres \\
--     -c "ALTER ROLE {ROLE} PASSWORD '<senha-forte>'"
--
-- E grave o DSN completo no `.env` (gitignored):
--
--   DATABASE_DSN=postgresql://{ROLE}:<senha>@{HOST}:{PORTA}/hermes_test
"""
    return sql.rstrip() + "\n" + aviso


def gravar_env(senha: str) -> bool:
    if not ENV.exists():
        print("ERRO: .env nao existe -- rode antes: Copy-Item .env.example .env")
        return False
    linhas = ENV.read_text(encoding="utf-8").splitlines()
    dsn = f"postgresql://{ROLE}:{senha}@{HOST}:{PORTA}/hermes_test"
    novas: list[str] = []
    trocou = False
    for linha in linhas:
        if linha.strip().startswith("DATABASE_DSN="):
            novas.append(f"DATABASE_DSN={dsn}")
            trocou = True
        elif linha.strip().startswith("HERMES_APP="):
            # config morta: nenhuma linha de codigo le esta variavel
            novas.append("# HERMES_APP removido: nenhuma linha de codigo le esta variavel.")
        else:
            novas.append(linha)
    if not trocou:
        novas.append(f"DATABASE_DSN={dsn}")
    ENV.write_text("\n".join(novas) + "\n", encoding="utf-8", newline="\n")
    return True


def main() -> int:
    sql = SQL.read_text(encoding="utf-8")
    senha = extrair_senha(sql)

    if senha is None:
        # Ja migrado? O SQL nao tem mais senha -- confirma pelo .env e sai limpo.
        env_txt = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
        m = re.search(rf"^DATABASE_DSN=postgresql://{ROLE}:([^@]+)@", env_txt, re.M)
        if m and not re.search(
            r"CREATE ROLE\s+\w+\s+LOGIN\s+PASSWORD", sql, re.I
        ):
            print("  ja migrado: senha esta no .env e ausente do SQL. Nada a fazer.")
            return 0
        print("ERRO: nao encontrei CREATE ROLE ... PASSWORD no SQL nem DSN no .env")
        return 1
    if senha == SENHA_PLACEHOLDER:
        print("ERRO: a senha ainda e o placeholder -- defina no SQL antes de rodar")
        return 1

    if not gravar_env(senha):
        return 1
    print("  .env: DATABASE_DSN preenchido (senha nao exibida)")

    SQL.write_text(limpar_sql(sql), encoding="utf-8", newline="\n")
    print("  sql/00_criar_banco.sql: senha removida, comando manual documentado")

    # Verificacao. Nao usar `senha not in sql`: se a senha for uma palavra comum
    # que aparece no proprio SQL (por exemplo, o nome de um owner citado na
    # secao de verificacao), o `in` da falso positivo -- foi exatamente o que
    # aconteceu na primeira execucao. O que importa e o statement ter sumido.
    sql_final = SQL.read_text(encoding="utf-8")
    ainda_tem = re.search(
        r"CREATE ROLE\s+\w+\s+LOGIN\s+PASSWORD\s+'[^']*'", sql_final, re.I
    )
    if ainda_tem:
        print("ERRO: CREATE ROLE ... PASSWORD ainda presente no SQL")
        return 1
    print("  verificacao: CREATE ROLE sem senha embutida")

    env_final = ENV.read_text(encoding="utf-8")
    if senha not in env_final:
        print("ERRO: senha nao chegou ao .env")
        return 1
    print("  verificacao: senha presente no .env")

    # Avaliacao de forca sem exibir nada.
    comuns = {"postgres", "admin", "root", "123456", "password", "senha", "test"}
    if senha.lower() in comuns:
        print(f"  ALERTA: senha tem {len(senha)} caracteres e e uma senha comum")
    elif len(senha) < 12:
        print(f"  ATENCAO: senha tem {len(senha)} caracteres (recomendado 16+)")
    else:
        print(f"  senha: {len(senha)} caracteres, nao e comum")
    return 0


if __name__ == "__main__":
    sys.exit(main())