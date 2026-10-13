"""Detecta mojibake (UTF-8 gravado duas vezes).

Existe porque o problema aconteceu de verdade: `Set-Content -Encoding UTF8` no
Windows PowerShell 5.1 grava BOM e, quando o arquivo passa pelo shell duas
vezes, produz `N\xc3O` no lugar de `Não` -- o segundo byte do `ã` se perde e o
dano e lossy, irrecuperavel por script.

Um script de reparo nao resolve: so a deteccao no CI e o portao. Por isso este
arquivo e um TESTE, e nao uma ferramenta.

`NÃO` em vez de `Não` e o caso real encontrado em README.md:99.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
EXTENSOES = {".py", ".md", ".yaml", ".yml", ".sql", ".toml"}
IGNORAR = {".venv", "__pycache__", ".git", "egg-info", ".ruff_cache", ".pytest_cache"}

# Marcadores de UTF-8 duplo. 'Ã'/'Â'/'ï¿½' so aparece quando bytes UTF-8 foram
# decodificados como latin-1 e re-codificados.
MARCADORES = ("\u00c3", "\u00c2", "\u00ef\u00bf\u00bd", "\ufffd")

# Este proprio arquivo contem os marcadores como literal (para afirmar contra
# eles), entao escaneá-lo acusaria o detector de ser o defeito.
AUTOREGRA = Path(__file__).name

ARQUIVOS = sorted(
    p
    for p in RAIZ.rglob("*")
    if p.is_file()
    and p.suffix in EXTENSOES
    and p.name != AUTOREGRA
    and not any(parte in IGNORAR for parte in p.parts)
)


def test_ha_arquivos_para_varrer() -> None:
    """Sem este assert, um glob quebrado deixa a suite passar em silencio."""
    assert len(ARQUIVOS) > 20, f"glob devolveu so {len(ARQUIVOS)} arquivos"


@pytest.mark.parametrize("caminho", ARQUIVOS, ids=lambda p: str(p.relative_to(RAIZ)))
def test_sem_mojibake(caminho: Path) -> None:
    texto = caminho.read_text(encoding="utf-8")
    for numero, linha in enumerate(texto.splitlines(), 1):
        for marcador in MARCADORES:
            if marcador in linha:
                nome = next(
                    (unicodedata.name(c, "?") for c in linha if c == marcador), "?"
                )
                pytest.fail(
                    f"{caminho.relative_to(RAIZ)}:{numero} contem mojibake "
                    f"({marcador!r} = {nome}). "
                    f"O conteudo e: {linha.strip()[:90]!r}"
                )


@pytest.mark.parametrize("caminho", ARQUIVOS, ids=lambda p: str(p.relative_to(RAIZ)))
def test_acentos_preservados_onde_deveriam(caminho: Path) -> None:
    """Cobre o caso lossy: `NÃO` passa no teste de marcadores (nao e mojibake
    detectavel) mas nao e portugues. Este teste exige acentuacao em arquivos
    que CLAIM estar em portugues."""
    if caminho.suffix != ".md":
        return
    nome = caminho.name
    if nome not in {"README.md", "runbook.md"}:
        return
    texto = caminho.read_text(encoding="utf-8")
    tem_acento = any(
        c in texto for c in "áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ"
    )
    assert tem_acento, f"{nome} nao tem nenhuma acentuacao -- suspeito de mojibake lossy"


@pytest.mark.parametrize("caminho", ARQUIVOS, ids=lambda p: str(p.relative_to(RAIZ)))
def test_sem_bom(caminho: Path) -> None:
    """BOM (U+FEFF) e invisivel no import e fatal no `ast.parse`.

    18 arquivos do projeto tinham BOM quando este teste foi escrito, todos
    criados por `Set-Content -Encoding UTF8` do Windows PowerShell 5.1. O
    sintoma aparece longe da causa: `pglast` e `ast.parse` quebram sem explicar
    nada sobre encoding.
    """
    bruto = caminho.read_bytes()
    assert not bruto.startswith(b"\xef\xbb\xbf"), (
        f"{caminho.relative_to(RAIZ)} comeca com BOM (U+FEFF). "
        "Rode: python scripts/normalizar_encoding.py"
    )


@pytest.mark.parametrize("caminho", ARQUIVOS, ids=lambda p: str(p.relative_to(RAIZ)))
def test_sem_crlf(caminho: Path) -> None:
    """CRLF em repo Linux nao quebra nada, mas torna diff ilegivel e faz
    `git diff --check` acusar whitespace em arquivo inteiro."""
    bruto = caminho.read_bytes()
    assert b"\r\n" not in bruto, (
        f"{caminho.relative_to(RAIZ)} tem CRLF. "
        "Rode: python scripts/normalizar_encoding.py"
    )


def test_bootstrap_sql_fora_de_migrations() -> None:
    """`Database.migrate()` le `migrations/*.sql` no BOOT do agente.

    Se o bootstrap (`CREATE ROLE`, `CREATE DATABASE`) estivesse la, o processo
    tentaria criar role e database sozinho, sem revisao humana, num servidor de
    producao com 49 bases de negocio.
    """
    bootstrap = RAIZ / "sql" / "00_criar_banco.sql"
    assert bootstrap.exists()
    for sql_file in (RAIZ / "migrations").glob("*.sql"):
        corpo = sql_file.read_text(encoding="utf-8").upper()
        for perigoso in ("CREATE DATABASE", "CREATE ROLE", "ALTER ROLE"):
            assert perigoso not in corpo, (
                f"{sql_file.name} contem '{perigoso}' -- e operacao de infraestrutura, "
                "nao migration da aplicacao"
            )


def test_readme_tem_a_secao_de_nao_verificado() -> None:
    """Trava a correcao especifica do caso lossy que encontramos.

    A forma quebrada e 'N' + U+00C3 + 'O' (o segundo byte do 'a' se perdeu).
    Construida por codepoint para nao reintroduzir mojibake neste arquivo.
    """
    readme = (RAIZ / "README.md").read_text(encoding="utf-8")
    correto = "O que N" + "\u00e3o est\u00e1 verificado"
    quebrado = "O que N" + "\u00c3O est\u00e1 verificado"
    assert correto in readme, "secao 'O que Não está verificado' ausente ou corrompida"
    assert quebrado not in readme, (
        "'NAO' indica mojibake lossy no titulo da secao (perdeu-se o segundo byte do 'a')"
    )