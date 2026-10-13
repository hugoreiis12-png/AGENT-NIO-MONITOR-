"""Remove simbolos publicos orfaos confirmados por `detectar_codigo_morto.py`.

NAO e uma ferramenta geral de refatoracao: cada remocao abaixo foi verificada
manualmente como nao-referenciada. `CollectorResult` foi preservado porque tem
uso real (4 referencias) -- foi o `Collector` (a ABC) que saiu, ja que nada
herdava dela.
"""

from __future__ import annotations

import pathlib
import re
import sys

RAIZ = pathlib.Path(__file__).resolve().parents[1]

# (arquivo, nome do simbolo) -> confirmado sem referencia
ORFAOS = [
    ("src/agent_nio_monitor/health.py", "medido"),
    ("src/agent_nio_monitor/health.py", "wait_until_ready"),
    ("src/agent_nio_monitor/notify/resolver.py", "resolver_para"),
    ("src/agent_nio_monitor/notify/templates.py", "primeira_linha_resposta"),
    ("src/agent_nio_monitor/sources/base.py", "Collector"),
    ("src/agent_nio_monitor/sources/base.py", "em_janela"),
    ("src/agent_nio_monitor/sources/n8n/collector.py", "record_failure_events"),
    ("src/agent_nio_monitor/store/repository.py", "minutes_ago"),
]


def remover(caminho: pathlib.Path, nome: str) -> bool:
    """Remove o bloco `def`/`class` inteiro, incluindo decorators e docstring."""
    linhas = caminho.read_text(encoding="utf-8").splitlines(keepends=True)
    inicio = None
    for i, linha in enumerate(linhas):
        if re.match(rf"^(async def|def|class)\s+{re.escape(nome)}\b", linha):
            inicio = i
            break
    if inicio is None:
        return False

    # sobe para incluir decoradores imediatamente acima
    while inicio > 0 and linhas[inicio - 1].lstrip().startswith("@"):
        inicio -= 1

    # acha o fim: proxima linha no mesmo nivel de indentacao (ou col 0)
    fim = inicio + 1
    while fim < len(linhas):
        linha = linhas[fim]
        if linha.strip() and not linha[0].isspace():
            break
        fim += 1

    # engole linhas em branco imediatamente seguintes
    while fim < len(linhas) and not linhas[fim].strip():
        fim += 1

    caminho.write_text("".join(linhas[:inicio] + linhas[fim:]), encoding="utf-8", newline="\n")
    return True


def main() -> int:
    removidos = 0
    for relativo, nome in ORFAOS:
        caminho = RAIZ / relativo
        if not caminho.exists():
            print(f"  AVISO: {relativo} nao existe")
            continue
        if remover(caminho, nome):
            removidos += 1
            print(f"  removido {nome} de {relativo}")
        else:
            print(f"  AVISO: {nome} nao encontrado em {relativo}")
    print(f"total removido: {removidos}/{len(ORFAOS)}")
    return 0 if removidos == len(ORFAOS) else 1


if __name__ == "__main__":
    sys.exit(main())