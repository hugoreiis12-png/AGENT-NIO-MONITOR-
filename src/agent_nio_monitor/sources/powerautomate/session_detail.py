"""Leitura detalhada de uma sessao de RPA: em que fase parou e em que acao.

Formatos verificados no ambiente RPA-CONTAS-A-RECEBER em 19/10/2026:
  - `rundetails` (JSON): `runtimeSteps` por fase (start, queue, assignment, connection,
    execution, ...) com `state`; `logDetails.uploadStatus` diz se o log de acoes ja subiu.
  - coluna de arquivo `additionalcontext`: `{"actions": [...]}`, cada acao com `name`,
    `functionName` (subfluxo), `index`, `status` e `insideErrorHandling`. Expira em ~28 dias.
NAO VERIFICADO com dado real: o campo de erro de uma acao com falha (nenhuma das 60 sessoes
de sucesso lidas tinha acao fora de `Succeeded`). Por isso `_erro_da_acao` tenta varias chaves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

STATUS_OK = frozenset({"Succeeded", "Skipped"})
CODIGOS_CONHECIDOS = {
    "NoCandidateMachine": "nenhuma maquina disponivel para rodar (maquina offline, sem sessao livre ou grupo vazio)",
    "WindowsIdentityIncorrect": "usuario ou senha da conexao da maquina estao errados",
}
MAX_ERRO = 300


@dataclass(frozen=True)
class AcaoRpa:
    nome: str
    subfluxo: str
    indice: int | None
    status: str
    tratada: bool
    erro: str | None

    def descricao(self) -> str:
        indice = f", acao #{self.indice}" if self.indice is not None else ""
        erro = f": {self.erro}" if self.erro else ""
        return f"{self.nome} (subfluxo {self.subfluxo}{indice}) -> {self.status}{erro}"


@dataclass(frozen=True)
class AnaliseAcoes:
    total: int
    tratadas: tuple[AcaoRpa, ...]
    falha: AcaoRpa | None


def _json(texto: str | bytes | None) -> dict[str, Any]:
    try:
        dados = json.loads(texto or "{}")
    except ValueError:
        return {}
    return dados if isinstance(dados, dict) else {}


def log_disponivel(rundetails: str | None) -> bool:
    return (_json(rundetails).get("logDetails") or {}).get("uploadStatus") == "Completed"


def fase_com_falha(rundetails: str | None) -> str | None:
    """Primeira fase do runtime que nao terminou em Success (ex.: 'assignment: Failed')."""
    for fase, estado in (_json(rundetails).get("runtimeSteps") or {}).items():
        situacao = (estado or {}).get("state")
        if situacao and situacao != "Success":
            return f"{fase}: {situacao}"
    return None


def explicar_codigo(codigo: str | None) -> str | None:
    return CODIGOS_CONHECIDOS.get(codigo or "")


def analisar_acoes(conteudo: bytes | str | None) -> AnaliseAcoes:
    acoes = [_acao(a) for a in _json(conteudo).get("actions") or [] if isinstance(a, dict)]
    ruins = [a for a in acoes if a.status not in STATUS_OK]
    return AnaliseAcoes(
        total=len(acoes),
        tratadas=tuple(a for a in ruins if a.tratada),
        falha=next((a for a in reversed(ruins) if not a.tratada), None),
    )


def _acao(bruta: dict[str, Any]) -> AcaoRpa:
    indice = bruta.get("index")
    return AcaoRpa(
        nome=str(bruta.get("name") or bruta.get("systemActionName") or "?"),
        subfluxo=str(bruta.get("functionName") or "main"),
        indice=int(indice) if isinstance(indice, int) else None,
        status=str(bruta.get("status") or "?"),
        tratada=bool(bruta.get("insideErrorHandling")),
        erro=_erro_da_acao(bruta),
    )


def _erro_da_acao(bruta: dict[str, Any]) -> str | None:
    for chave in ("error", "errorMessage", "exception", "message"):
        valor = bruta.get(chave)
        if isinstance(valor, dict):
            valor = valor.get("message") or valor.get("Message") or json.dumps(valor, ensure_ascii=False)
        if valor:
            return str(valor)[:MAX_ERRO]
    return None
