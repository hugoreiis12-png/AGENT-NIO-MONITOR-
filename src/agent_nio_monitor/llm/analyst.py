"""Analista de alertas: transforma os fatos do evento em resumo, causa e passos.

O LLM analisa e redige; severidade e destinatario nao sao dele (ADR D3). Saida
invalida, vazia ou com numero que nao esta nos fatos vira fallback deterministico.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

import structlog

from .client import LlmClient, LLMUnavailable
from .facts import fatos

log = structlog.get_logger(__name__)

NUMERO_ISOLADO = r"(?<![A-Za-z0-9_])\d+(?![A-Za-z0-9_])"
MAX_RESUMO, MAX_CAUSA, MAX_PASSO, MAX_PASSOS = 250, 400, 200, 5

SYSTEM = """Voce e um engenheiro de plataforma que analisa alertas de n8n e Docker.
Responda SOMENTE um objeto JSON com as chaves:
- resumo: 1 frase direta com o que quebrou e onde.
- causa_provavel: 1 a 2 frases. Se os fatos nao permitem concluir, diga que a causa nao foi determinada.
- como_resolver: lista de 2 a 4 passos curtos, no imperativo, em ordem de execucao.

Regras: use SOMENTE os fatos fornecidos. Nao invente data, hora, numero, host, porta nem nome.
Sem emoji, sem markdown, sem enrolacao. Portugues do Brasil."""

USER = """Fatos do alerta (ja redigidos, sem segredo):

Evento: {tipo}
Titulo: {titulo}
Severidade: {severidade}
{fatos}"""


@dataclass
class Analysis:
    resumo: str
    causa_provavel: str
    como_resolver: list[str]
    # Preenchidos so pela investigacao com ferramentas: trechos conferidos e o grau de certeza.
    evidencias: list[str] = field(default_factory=list)
    confianca: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def abertura_fallback(contexto: dict) -> str:
    """Texto deterministico: o alerta precisa sair mesmo com o LLM fora."""
    if contexto.get("titulo") and not contexto.get("ocorrencias"):
        return f"{contexto['titulo']}."
    nome = contexto.get("workflow_nome") or contexto.get("recurso") or "recurso"
    partes = [f"{nome} falhou {contexto.get('ocorrencias', 1)} vez(es)"]
    if contexto.get("primeira"):
        partes.append(f"desde {contexto['primeira']}")
    frase = " ".join(partes) + "."
    if contexto.get("erro"):
        frase += f" Erro: {contexto['erro']}."
    return frase


def _texto(valor: object, limite: int) -> str | None:
    if not isinstance(valor, str) or not valor.strip():
        return None
    return valor.strip()[:limite]


def _validar(dados: dict) -> Analysis | None:
    resumo = _texto(dados.get("resumo"), MAX_RESUMO)
    causa = _texto(dados.get("causa_provavel"), MAX_CAUSA)
    brutos = dados.get("como_resolver")
    passos = [p for p in (_texto(x, MAX_PASSO) for x in brutos) if p] if isinstance(brutos, list) else []
    if not (resumo and causa and passos):
        return None
    return Analysis(resumo, causa, passos[:MAX_PASSOS])


def _inventou_numero(analise: Analysis, base: str) -> bool:
    """Um modelo preenche lacuna com data/hora plausivel. Numero fora dos fatos = rejeita."""
    texto = " ".join([analise.resumo, analise.causa_provavel, *analise.como_resolver])
    return any(n not in base for n in re.findall(NUMERO_ISOLADO, texto))


class Investigador(Protocol):
    async def investigar(self, contexto: dict, fonte: str) -> Any: ...


class AlertAnalyst:
    def __init__(self, client: LlmClient, *, enabled: bool = True, investigador: Investigador | None = None) -> None:
        self.client = client
        self.enabled = enabled
        self.investigador = investigador

    async def analyze(self, contexto: dict) -> tuple[Analysis | None, str]:
        """(analise, status). status in {'investigado', 'llm', 'fallback', 'desligado'}; analise e
        None sempre que o chamador deve usar o texto deterministico. A investigacao com
        ferramentas vem primeiro; se ela nao se sustentar, cai na analise so dos fatos."""
        if not self.enabled:
            return None, "desligado"
        if self.investigador is not None:
            investigada = await self.investigador.investigar(contexto, str(contexto.get("fonte") or ""))
            if investigada is not None:
                return investigada, "investigado"
        prompt = USER.format(
            tipo=contexto.get("tipo", "-"), titulo=contexto.get("titulo", "-"),
            severidade=contexto.get("severidade", "-"), fatos=fatos(contexto),
        )
        try:
            dados = await self.client.chat_json(SYSTEM, prompt)
        except LLMUnavailable as exc:
            log.warning("llm_indisponivel_usando_fallback", erro=str(exc))
            return None, "fallback"
        analise = _validar(dados)
        if analise is None:
            log.warning("saida_do_llm_rejeitada_formato", chaves=sorted(dados))
            return None, "fallback"
        if _inventou_numero(analise, prompt):
            log.warning("saida_do_llm_rejeitada_numero_inventado")
            return None, "fallback"
        return analise, "llm"
