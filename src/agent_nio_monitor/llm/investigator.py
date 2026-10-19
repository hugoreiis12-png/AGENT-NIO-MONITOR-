"""Qwen investiga antes de redigir: chama ferramentas de leitura, junta evidencia e conclui (ADR D42).

O modelo continua sem decidir severidade, destinatario ou se o alerta sai (ADR D3). O que muda
e a CAUSA: em vez de deduzir so dos fatos do evento, ele le log, execucao e acoes do RPA.
Toda evidencia citada precisa aparecer LITERALMENTE numa saida de ferramenta ou nos fatos; o
resto e descartado, e sem evidencia conferida a confianca cai para "baixa".
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Protocol

import structlog

from .analyst import Analysis, _inventou_numero, _validar
from .client import LlmClient, LLMUnavailable
from .facts import fatos

log = structlog.get_logger(__name__)

CONFIANCAS = ("alta", "media", "baixa")
MAX_CHAMADAS_POR_RODADA = 3
MIN_EVIDENCIA = 12
MAX_EVIDENCIAS = 4
IDENTIFICADORES = ("workflow_id", "execucao_id", "fluxo_id", "sessao_id", "recurso", "stack", "endpoint_id")

SYSTEM = """Voce e o Hermes, investigador de incidentes de n8n, Docker/Portainer e Power Automate (RPA).
Antes de concluir, USE AS FERRAMENTAS para buscar evidencia: log, execucao, estado do container,
acoes do RPA. Compare com o padrao (execucoes anteriores) para separar causa de sintoma.
Quando tiver evidencia suficiente, pare de chamar ferramentas.
Use SOMENTE o que os fatos e as ferramentas mostraram. Nao invente data, hora, numero, host nem nome.
Portugues do Brasil, direto, sem emoji e sem markdown."""

USER = """Alerta a investigar (fatos ja redigidos):

Evento: {tipo}
Titulo: {titulo}
Severidade: {severidade}
{fatos}

Identificadores para as ferramentas: {ids}"""

PEDIDO_FINAL = """Conclua. Responda SOMENTE um objeto JSON com as chaves:
- resumo: 1 frase com o que quebrou e onde.
- causa_provavel: 1 a 2 frases com a causa RAIZ apurada (nao o sintoma). Sem evidencia, diga que nao foi determinada.
- evidencias: lista de 1 a 4 trechos COPIADOS LITERALMENTE das saidas das ferramentas ou dos fatos.
- confianca: "alta", "media" ou "baixa".
- como_resolver: lista de 2 a 4 passos curtos, no imperativo, em ordem."""


class Ferramentas(Protocol):
    def esquemas(self, fonte: str) -> list[dict]: ...
    async def executar(self, nome: str, argumentos: str | dict) -> str: ...


def _normalizar(texto: str) -> str:
    return re.sub(r"\s+", " ", texto).strip().lower()


def evidencias_conferidas(citadas: Any, base: str) -> list[str]:
    """So o que aparece literalmente (a menos de espaco/caixa) no que o modelo realmente leu."""
    if not isinstance(citadas, list):
        return []
    alvo = _normalizar(base)
    validas = [str(c).strip() for c in citadas if isinstance(c, str) and len(c.strip()) >= MIN_EVIDENCIA]
    return [c for c in validas if _normalizar(c) in alvo][:MAX_EVIDENCIAS]


class Investigator:
    def __init__(self, client: LlmClient, ferramentas: Ferramentas, *, max_passos: int = 4, prazo: float = 75.0):
        self.client, self.ferramentas = client, ferramentas
        self.max_passos, self.prazo = max_passos, prazo

    async def investigar(self, contexto: dict, fonte: str) -> Analysis | None:
        """None quando nao ha ferramenta para a fonte, o modelo falha ou a saida nao se sustenta."""
        esquemas = self.ferramentas.esquemas(fonte)
        if not esquemas:
            return None
        prompt = USER.format(
            tipo=contexto.get("tipo", "-"), titulo=contexto.get("titulo", "-"),
            severidade=contexto.get("severidade", "-"), fatos=fatos(contexto), ids=_ids(contexto),
        )
        mensagens: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
        lido = [prompt]
        try:
            async with asyncio.timeout(self.prazo):
                await self._rodadas(mensagens, esquemas, lido)
                dados = await self.client.chat_json_messages([*mensagens, {"role": "user", "content": PEDIDO_FINAL}])
        except (LLMUnavailable, TimeoutError) as exc:
            log.warning("investigacao_interrompida", erro=type(exc).__name__, chamadas=len(lido) - 1)
            return None
        return _concluir(dados, "\n".join(lido), chamadas=len(lido) - 1)

    async def _rodadas(self, mensagens: list[dict[str, Any]], esquemas: list[dict], lido: list[str]) -> None:
        for _ in range(self.max_passos):
            resposta = await self.client.chat_tools(mensagens, esquemas)
            chamadas = (resposta.get("tool_calls") or [])[:MAX_CHAMADAS_POR_RODADA]
            if not chamadas:
                return
            mensagens.append({"role": "assistant", "content": resposta.get("content"), "tool_calls": chamadas})
            for chamada in chamadas:
                funcao = chamada.get("function") or {}
                saida = await self.ferramentas.executar(str(funcao.get("name")), funcao.get("arguments") or "{}")
                lido.append(saida)
                mensagens.append({"role": "tool", "tool_call_id": chamada.get("id"), "content": saida})


def _ids(contexto: dict) -> str:
    pares = [f"{chave}={contexto[chave]}" for chave in IDENTIFICADORES if contexto.get(chave) not in (None, "")]
    return ", ".join(pares) or "(nenhum)"


def _concluir(dados: dict, base: str, *, chamadas: int) -> Analysis | None:
    analise = _validar(dados)
    if analise is None or _inventou_numero(analise, base):
        log.warning("investigacao_rejeitada", motivo="formato" if analise is None else "numero_inventado")
        return None
    analise.evidencias = evidencias_conferidas(dados.get("evidencias"), base)
    confianca = str(dados.get("confianca") or "").lower()
    analise.confianca = confianca if confianca in CONFIANCAS and analise.evidencias else "baixa"
    log.info("investigacao_concluida", chamadas=chamadas, evidencias=len(analise.evidencias),
             confianca=analise.confianca, descartadas=_descartadas(dados, analise))
    return analise


def _descartadas(dados: dict, analise: Analysis) -> int:
    citadas = dados.get("evidencias")
    return (len(citadas) if isinstance(citadas, list) else 0) - len(analise.evidencias)

