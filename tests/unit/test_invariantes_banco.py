"""Invariantes que so o banco real revelou, agora travados sem banco.

Cada teste aqui cobre um defeito que passou em 476 testes unitarios e so
apareceu quando o codigo passou a executar contra o PostgreSQL.

Regra desta suite: nao precisa de banco. Um bug que so aparece com banco e um
bug cujo teste tambem so funciona com banco -- no dia que o banco estiver fora,
ele volta sem aviso. Estes testes leem o codigo e o SQL.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[2]
SRC = RAIZ / "src"
MIGRATIONS = RAIZ / "migrations"


def _arquivos_py() -> list[pathlib.Path]:
    return sorted(SRC.rglob("*.py"))


class TestExecutemanyNoCursor:
    """`conn.executemany` levanta AttributeError no psycopg3.

    `executemany` vive no CURSOR. Apareceu em `save_event` e `save_capabilities`,
    em codigo com 0% de cobertura que nunca tinha rodado.
    """

    @pytest.mark.parametrize("caminho", _arquivos_py(), ids=lambda p: p.name)
    def test_nenhum_executemany_em_connection(self, caminho: pathlib.Path) -> None:
        arvore = ast.parse(caminho.read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            if (
                isinstance(no, ast.Call)
                and isinstance(no.func, ast.Attribute)
                and no.func.attr == "executemany"
                and isinstance(no.func.value, ast.Name)
                and no.func.value.id == "conn"
            ):
                pytest.fail(
                    f"{caminho.name}:{no.lineno} chama conn.executemany -- "
                    "no psycopg3 isso e conn.cursor().executemany"
                )

    def test_uso_correto_existe_em_algum_lugar(self) -> None:
        """O outro lado da regra: existe pelo menos um `cursor().executemany`,
        para que a forma correta tenha referencia e nao suma sozinha."""
        achou = any(
            "cur.executemany" in c.read_text(encoding="utf-8")
            for c in _arquivos_py()
        )
        assert achou, (
            "nenhum uso de cursor().executemany no codigo -- se o padrao certo "
            "sumiu, o teste anterior passa a falhar sem ninguem saber onde"
        )


class TestPlaceholderDeTabela:
    """`INSERT INTO {schema}` sem o nome da tabela produz
    `INSERT INTO "hermes"` e falha com 'relacao hermes nao existe'.

    Sintoma enganoso: parece problema de schema, nao de SQL malformado.
    """

    @pytest.mark.parametrize("caminho", _arquivos_py(), ids=lambda p: p.name)
    def test_insert_tem_nome_de_tabela(self, caminho: pathlib.Path) -> None:
        fonte = caminho.read_text(encoding="utf-8")
        for achado in re.finditer(r"INSERT\s+INTO\s+(\{?\w+\}?)", fonte, re.I):
            destino = achado.group(1)
            assert destino not in ("{schema}", '"hermes"'), (
                f"{caminho.name}: INSERT INTO {destino} -- falta o nome da tabela "
                "depois do schema"
            )

    def test_set_cursor_usa_helper_de_tabela(self) -> None:
        fonte = (SRC / "agent_nio_monitor" / "store" / "postgres_repository.py").read_text(
            encoding="utf-8"
        )
        assert "INSERT INTO {t} (source, scope, cursor" in fonte, (
            "set_cursor voltou a usar {schema} em vez de {t} com self._t(...)"
        )


class TestMigrations:
    @pytest.mark.parametrize("arquivo", sorted(MIGRATIONS.glob("*.sql")), ids=lambda p: p.name)
    def test_migration_nao_cria_schema(self, arquivo: pathlib.Path) -> None:
        """`CREATE SCHEMA` exige CREATE no DATABASE -- privilegio muito mais largo
        que CREATE na schema, e o agente tem so o segundo.

        Verificado: com `CREATE SCHEMA IF NOT EXISTS hermes` na 001, a migration
        inteira abortava com 'permissao negada para banco de dados', mesmo tendo
        IF NOT EXISTS. Criar schema e infraestrutura: mora em `sql/`.
        """
        corpo = arquivo.read_text(encoding="utf-8")
        sem_comentario = "\n".join(
            l for l in corpo.splitlines() if not l.strip().startswith("--")
        )
        assert "CREATE SCHEMA" not in sem_comentario.upper(), (
            f"{arquivo.name} contem CREATE SCHEMA -- isso pertence a "
            "sql/00_criar_banco.sql (ver ADR D11)"
        )

    @pytest.mark.parametrize("arquivo", sorted(MIGRATIONS.glob("*.sql")), ids=lambda p: p.name)
    def test_migration_nao_cria_database_nem_role(self, arquivo: pathlib.Path) -> None:
        sem_comentario = "\n".join(
            l for l in arquivo.read_text(encoding="utf-8").splitlines()
            if not l.strip().startswith("--")
        ).upper()
        for perigoso in ("CREATE DATABASE", "CREATE ROLE", "ALTER ROLE"):
            assert perigoso not in sem_comentario, (
                f"{arquivo.name} contem {perigoso} -- operacao de infraestrutura, "
                "nao migration da aplicacao"
            )


class TestFuncaoDeParticoes:
    """A 001 criava `execution_2026_09`, `execution_2026_11` e `execution_2027_02`:
    outubro FALTANDO. Todo INSERT de outubro morria com CheckViolation.

    Duas causas: `start_d` mutado dentro do laco E somado com `i * INTERVAL`; e
    limites calculados no fuso da sessao em vez de UTC.
    """

    @staticmethod
    def _corpo_da_funcao(nome: str) -> str:
        for arquivo in MIGRATIONS.glob("*.sql"):
            texto = arquivo.read_text(encoding="utf-8")
            m = re.search(
                rf"CREATE OR REPLACE FUNCTION hermes\.{nome}\(.*?\nEND;\n\$\$;",
                texto,
                re.S,
            )
            if m:
                return m.group(0)
        pytest.skip(f"funcao {nome} nao encontrada nas migrations")

    @pytest.mark.parametrize(
        "nome", ["ensure_partitions", "drop_partitions_older_than"]
    )
    def test_existe_versao_em_migration(self, nome: str) -> None:
        assert self._corpo_da_funcao(nome)

    def test_laco_nao_muta_a_base(self) -> None:
        corpo = self._corpo_da_funcao("ensure_partitions")
        dentro_do_laco = corpo.split("FOR i IN", 1)[1]
        mutacoes = re.findall(r"^\s*(\w+)\s*:=", dentro_do_laco, re.M)
        for nome in mutacoes:
            assert nome != "base_mes", (
                "base_mes esta sendo mutada dentro do laco -- foi assim que "
                "meio mes ficou sem particao"
            )
        assert "base_mes + i * INTERVAL '1 month'" in corpo, (
            "o inicio da particao tem de ser derivado de base_mes + i"
        )

    def test_tabela_criada_e_qualificada_com_schema(self) -> None:
        corpo = self._corpo_da_funcao("ensure_partitions")
        assert re.search(
            r"CREATE TABLE IF NOT EXISTS %I\.%I PARTITION OF", corpo
        ), (
            "a tabela de particao precisa ser qualificada com schema: sem o "
            "prefixo, o CREATE usa o search_path e falha com 'permissao negada "
            "para esquema public'"
        )

    def test_limites_em_utc_sem_conversao_dupla(self) -> None:
        corpo = self._corpo_da_funcao("ensure_partitions")
        assert "AT TIME ZONE 'UTC'" in corpo
        assert "::timestamptz AT TIME ZONE" not in corpo, (
            "date::timestamptz usa o fuso da sessao e AT TIME ZONE converte de "
            "novo -- o limite sai 03:00 em vez de 00:00. Use ::timestamp AT TIME ZONE"
        )

    def test_base_em_utc(self) -> None:
        corpo = self._corpo_da_funcao("ensure_partitions")
        assert "now() AT TIME ZONE 'UTC'" in corpo, (
            "a base do calculo tem de ser UTC: limite de particao e instante "
            "absoluto, e o fuso da sessao desloca a fronteira"
        )


class TestNomesDeStatusETipos:
    """`ck_event_severity_conhecida` e `ck_notification_status_conhecido` na 002
    listam valores. Divergencia entre o SQL e o codigo faz o banco recusar
    escrita legitima -- o pior tipo de bug de constraint."""

    def test_severidades_do_codigo_batem_com_a_migration(self) -> None:
        from agent_nio_monitor.notify.templates import SEV_LABEL

        sql = (MIGRATIONS / "002_invariantes.sql").read_text(encoding="utf-8")
        m = re.search(r"severity IN \(([^)]+)\)", sql)
        assert m, "ck_event_severity_conhecida nao encontrada"
        no_sql = set(re.findall(r"'([a-z]+)'", m.group(1)))
        assert no_sql == set(SEV_LABEL), (
            f"divergencia: SQL={sorted(no_sql)} codigo={sorted(SEV_LABEL)}"
        )

    def test_status_de_notificacao_batem_com_a_migration(self) -> None:
        """A constraint 002 lista os status aceitos. Se o codigo produzir um
        status fora da lista, o banco recusa uma notificacao legitima -- o pior
        tipo de bug de constraint, porque so aparece em producao.

        Varre os módulos que CONSTRUEM o DispatchResult (`DispatchResult` em si
        e so uma dataclass sem corpo).
        """
        sql = (MIGRATIONS / "002_invariantes.sql").read_text(encoding="utf-8")
        m = re.search(r"status IN \(([^)]+)\)", sql)
        assert m, "ck_notification_status_conhecido nao encontrada"
        no_sql = set(re.findall(r"'([a-z_]+)'", m.group(1)))

        alvos = [
            SRC / "agent_nio_monitor" / "notify" / "mailer.py",
            SRC / "agent_nio_monitor" / "main.py",
        ]
        no_codigo: set[str] = set()
        for alvo in alvos:
            no_codigo |= set(re.findall(r'status="(\w+)"', alvo.read_text(encoding="utf-8")))

        assert no_codigo, "nao encontrei nenhum DispatchResult(status=...) no codigo"
        assert no_codigo <= no_sql, (
            f"o codigo produz status que a constraint rejeita: "
            f"produz={sorted(no_codigo - no_sql)} constraint={sorted(no_sql)}"
        )