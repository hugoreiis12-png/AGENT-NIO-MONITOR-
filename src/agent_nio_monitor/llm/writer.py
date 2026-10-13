"""Cliente do LLM local (Ollama) e o redator.

REGRA (ADR D3): o LLM **redige**. Nao decide severidade, nao decide destinatario,
nao decide se ha alerta. Se o LLM cair, o alerta tem que sair do mesmo jeito --
entao `AlertWriter.write()` tem fallback deterministico e nunca levanta.

Medido na maquina do dono: hermes3:8b (llama, 4.34 GB) ~88 ms/token.
Um alerta de 300 palavras custa ~2,5 s. Aceitavel para um caminho que nao e
critico.
"""

from __future__ import annotations

import httpx
import structlog

log = structlog.get_logger(__name__)

SYSTEM = """Voce escreve o paragrafo de abertura de um alerta de operacao.

Regras:
- Direto. Comece pelo que quebrou e desde quando.
- Sem enrolacao, sem "esperamos que esteja bem", sem linguagem de vendor.
- Fatos apenas. Nao invente causa, impacto ou numero que nao esta no contexto.
- Se o contexto nao diz a causa, diga que a causa nao foi determinada.
- Maximo 3 frases. Portugues do Brasil.
- Nao use emoji, markdown ou titulo.

Responda SOMENTE com o paragrafo."""

USER = """Contexto do alerta (ja redigido, sem segredo):

Projeto: {projeto}
Recurso: {recurso}
Severidade: {severidade}
Ocorrencias: {n}
Primeira falha: {primeira}
Ultima falha: {ultima}
Erro: {erro}
Modo de execucao: {modo}

Escreva o paragrafo de abertura."""


class LLMUnavailable(RuntimeError):
    pass


class LlmClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = 60.0,
        num_ctx: int = 8192,
        temperature: float = 0.2,
        max_tokens: int = 400,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def generate(self, prompt: str, *, system: str = SYSTEM) -> str:
        # NAO enviar "format". Verificado contra o Ollama em 192.168.0.140:11434:
        # a versao instalada responde HTTP 500 quando "format" vem no
        # /api/generate. Somente o teste contra o modelo real pega isso -- um
        # mock passaria sempre.
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {
                "num_ctx": self.num_ctx,
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
        }
        try:
            resp = await self._client.post(f"{self.base_url}/api/generate", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc

        texto = (data.get("response") or "").strip()
        if not texto:
            raise LLMUnavailable("LLM devolveu resposta vazia")
        return texto

    async def healthy(self) -> bool:
        try:
            resp = await self._client.get(f"{self.base_url}/api/tags", timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False


def _fallback(contexto: dict) -> str:
    """Texto deterministico. Precisa ser utilizavel sozinho: e o que sai quando
    o LLM esta fora, e perder o alerta e pior que perder a eloquencia."""
    nome = contexto.get("workflow_nome") or contexto.get("recurso") or "recurso"
    n = contexto.get("n", 1)
    partes = [f"{nome} falhou {n} vez(es)"]
    if contexto.get("primeira"):
        partes.append(f"desde {contexto['primeira']}")
    frase = " ".join(partes) + "."
    if contexto.get("erro"):
        frase += f" Erro: {contexto['erro']}."
    return frase


class AlertWriter:
    def __init__(self, client: LlmClient, *, enabled: bool = True) -> None:
        self.client = client
        self.enabled = enabled

    async def write(self, contexto: dict) -> tuple[str, str]:
        """Devolve (abertura_natural, status).

        status in {'llm', 'fallback', 'desligado'}. Vai para o log para saber
        quantas vezes a eloquencia foi comprada com queda do LLM.
        """
        if not self.enabled:
            return _fallback(contexto), "desligado"
        try:
            prompt = USER.format(
                projeto=contexto.get("projeto", "-"),
                recurso=contexto.get("recurso", "-"),
                severidade=contexto.get("severidade", "-"),
                n=contexto.get("n", 1),
                primeira=contexto.get("primeira", "-"),
                ultima=contexto.get("ultima", "-"),
                erro=contexto.get("erro") or "(sem mensagem de erro)",
                modo=contexto.get("modo", "?"),
            )
            texto = await self.client.generate(prompt)
            # Um 8B pode devolver vazio, ou ecoar o prompt. Sanidade minima:
            # o texto precisa existir, ser curto, e nao conter instrucao.
            if not texto.strip() or len(texto) > 2000 or "Voce escreve o paragrafo" in texto:
                log.warning("saida_do_llm_rejeitada", tamanho=len(texto))
                return _fallback(contexto), "fallback"
            return texto, "llm"
        except LLMUnavailable as exc:
            log.warning("llm_indisponivel_usando_fallback", erro=str(exc))
            return _fallback(contexto), "fallback"