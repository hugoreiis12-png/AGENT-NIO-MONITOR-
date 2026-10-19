"""Cliente n8n.

Fatos verificados nesta instancia que moldam o cliente:
  - header e `X-N8N-API-KEY`, nao `Authorization: Bearer` (o doc original errava)
  - `GET /api/v1/executions` aceita status IN {error, success, waiting}
  - paginacao por `cursor`, e nao ha `startedAfter` confiavel -> polling por cursor
  - o summary traz `startedAt`/`stoppedAt`, nao `duration_ms` -> derivamos
"""

from __future__ import annotations

from typing import Any
from collections.abc import Iterator

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

log = structlog.get_logger(__name__)


class N8NApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class CursorInvalid(N8NApiError):
    """A cursor aponta pra registro removido pelo prune do n8n. Sinaliza BURACO:
    nao e erro a silenciar, e perda de dado que precisa de alerta."""


def _deve_retryar(exc: BaseException) -> bool:
    """Transitorio ou nao?

    A versao anterior so repetia em `TimeoutException` e `NetworkError`. Faltava o
    caso que mais importa na pratica: durante um restart do n8n o gateway responde
    502/503, e e exatamente entao que o monitor precisa insistir em vez de registrar
    erro e passar 60 s as cegas.

    Nao repete em 4xx: `CursorInvalid` (404) e erro de parametro (400) nao melhoram
    com tentativa -- cursor invalidado precisa de reset, parametro invalido precisa
    de correcao.

    `httpx.HTTPStatusError` tambem entra: cobre qualquer caminho que chegue a
    `raise_for_status()` sem passar pelo tratamento explicito de codigo.
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    status = getattr(exc, "status", None)
    if status is None and isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    if isinstance(status, int):
        return status >= 500 or status == 429
    return False


def _n8n_responde(exc: BaseException) -> bool:
    """O n8n esta de pe, independente do retry? Reutiliza o mesmo juizo de `_deve_retryar`.

    Mesma politica, resposta diferente: uma falha que MERECE retentativa e
    exatamente a que significa "o n8n nao esta responding agora". Reutilizar a
    funcao evita duas listas de status que divergem -- e divergir aqui daria o
    pior resultado: o agenteInsetsiste e porem reporta o n8n como de pe.

    4xx NAO conta como fora: chave invalida (401) ou falta de permissao (403) e
    problema de credencial, e o n8n esta perfeitamente de pe para atender quem
    tem acesso.
    """
    return _deve_retryar(exc)


class N8NClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        verify_ssl: bool = True,
        page_size: int = 100,
        max_pages: int = 50,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self.max_pages = max_pages
        # Alcancabilidade observada. Comeca otimista porque `None` (nunca falhou)
        # e indistinguivel de "de pe" para quem so precisa decidir se usa e-mail
        # ou log -- e ate a primeira resposta chegar, o n8n quase sempre esta de pe.
        # Quem mirror no `HealthState` e o `Agent`, depois de cada ciclo.
        self.alive: bool = True
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-N8N-API-KEY": api_key, "Accept": "application/json"},
            timeout=timeout,
            verify=verify_ssl,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> N8NClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ crud

    @retry(
        retry=retry_if_exception(_deve_retryar),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
        reraise=True,
    )
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            resp = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            self.alive = False
            raise N8NApiError(f"erro de rede em {path}: {type(exc).__name__}") from exc

        # Qualquer resposta HTTP chegou: o n8n respondeu. A distincao de 4xx vem
        # abaixo, onde 401/403 significam problema de credencial, nao queda.
        if resp.status_code < 500:
            self.alive = True

        if resp.status_code in (401, 403):
            # 401 e 403 sao diagnosticos diferentes e a distincao importa: 401
            # significa que a chave nao foi aceita (token invalido, expirado ou
            # de outra instancia) -- refazer a credencial nao resolve; 403
            # significa que a chave foi aceita e falta o papel. Colapsar os dois
            # mandava o operador caçar permissao num token invalido.
            raise N8NApiError(
                f"{resp.status_code} em {path}: "
                + (
                    "chave NAO aceita -- token invalido, expirado ou de outra "
                    "instancia. Verifique o endereco ANTES de refazer a credencial."
                    if resp.status_code == 401
                    else "chave aceita, mas o usuario nao tem o papel exigido "
                    "(owner/admin)."
                ),
                resp.status_code,
            )
        if resp.status_code == 400:
            # Cursor invalida pode voltar como 400 e nao 404, dependendo da
            # versao. Sem diferenciar, o coletor trata como erro transitorio e
            # o buraco nunca e reportado -- que e o pior resultado possivel.
            texto = resp.text.lower()
            cursor_culpa = any(m in texto for m in ("cursor", "pagination", "invalid"))
            raise (CursorInvalid if cursor_culpa else N8NApiError)(
                f"400 em {path}: {resp.text[:200]}", 400
            )
        if resp.status_code >= 500:
            self.alive = False
            raise N8NApiError(f"{resp.status_code} em {path}", resp.status_code)
        if resp.status_code == 404:
            raise CursorInvalid(f"404 em {path}: recurso removido", 404)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------ paginacao

    async def _iter_pages(
        self, path: str, params: dict[str, Any], cursor: str | None = None
    ) -> Iterator[dict[str, Any]]:
        params = {**params, "limit": self.page_size}
        atual = cursor
        for _ in range(self.max_pages):
            if atual:
                params["cursor"] = atual
            data = await self._get(path, params)
            yield data
            atual = data.get("nextCursor")
            if not atual:
                return

    async def iter_executions(
        self,
        *,
        status: str | None = "error",
        workflow_id: str | None = None,
        cursor: str | None = None,
        include_data: bool = False,
    ) -> Iterator[dict[str, Any]]:
        async for page in self.iter_executions_pages(
            status=status, workflow_id=workflow_id, cursor=cursor, include_data=include_data
        ):
            for item in page.get("data", []):
                yield item

    async def iter_executions_pages(
        self,
        *,
        status: str | None = "error",
        workflow_id: str | None = None,
        cursor: str | None = None,
        include_data: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Paginas cruas de `/executions`, com `nextCursor`.

        Publico porque o coletor PRECISA do `nextCursor`: e ele que vira o cursor
        de retomada gravado em `source_cursor`. `iter_executions()` achata a
        pagina e perde esse campo, entao o coletor acabava chamando o PRIVADO
        `_iter_pages` e acorrentando o coletor mais critico do agente ao
        detalhe interno de paginacao deste modulo.

        Qualquer refatoracao de `_iter_pages` quebraria o coletor silenciosamente
        -- que e a razao de o acesso privado ter virado metodo publico aqui.
        """
        params: dict[str, Any] = {"includeData": "true" if include_data else "false"}
        if status:
            params["status"] = status
        if workflow_id:
            params["workflowId"] = workflow_id
        async for page in self._iter_pages("/api/v1/executions", params, cursor):
            yield page

    async def iter_workflows(
        self,
        *,
        active: bool | None = None,
        project_id: str | None = None,
        cursor: str | None = None,
        exclude_pinned_data: bool = True,
    ) -> Iterator[dict[str, Any]]:
        """Aviso de custo: a pagina completa devolveu 11,2 MB na instancia real.
        Por isso `workflow_full_seconds` e 900 e nao 60 -- ver ADR D6."""
        params: dict[str, Any] = {"excludePinnedData": str(exclude_pinned_data).lower()}
        if active is not None:
            params["active"] = str(active).lower()
        if project_id:
            params["projectId"] = project_id
        async for page in self._iter_pages("/api/v1/workflows", params, cursor):
            for item in page.get("data", []):
                yield item

    async def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        return await self._get(f"/api/v1/workflows/{workflow_id}")

    async def iter_credentials(self) -> Iterator[dict[str, Any]]:
        """Credenciais do n8n, paginadas.

        Publico porque `CredentialCollector` precisa de todas, e nao so da
        pagina 1. O campo `shared` traz e-mail de quem tem acesso -- o coletor
        guarda APENAS `id`, `name`, `type` e `updatedAt`, e joga `shared`
        fora. Nao ha o que redactar: o dado sensivel nunca chega a ser
        guardado.
        """
        async for page in self._iter_pages("/api/v1/credentials", {}):
            for item in page.get("data", []):
                yield item

    async def workflow_history(self, workflow_id: str) -> list[dict[str, Any]] | None:
        """Existe a partir de certa versao. O capability probe decide se chama.
        Retorna None quando ausente -- nao lanca, porque ausencia e um cenario
        legitimo e conhecido, nao um erro."""
        try:
            data = await self._get(f"/api/v1/workflows/{workflow_id}/history")
        except N8NApiError as exc:
            if exc.status in (404, 405):
                return None
            raise
        return list(data.get("data", []))

    async def list_projects(self) -> list[dict[str, Any]]:
        return list((await self._get("/api/v1/projects")).get("data", []))

    async def list_project_users(self, project_id: str) -> list[dict[str, Any]]:
        return list(
            (await self._get(f"/api/v1/projects/{project_id}/users")).get("data", [])
        )

    async def list_users(self) -> list[dict[str, Any]]:
        return list((await self._get("/api/v1/users")).get("data", []))

    async def last_execution(self, workflow_id: str) -> dict[str, Any] | None:
        """Execucao mais recente do workflow, de qualquer status (sem o payload)."""
        dados = await self.recent_executions(workflow_id, 1)
        return dados[0] if dados else None

    async def recent_executions(self, workflow_id: str, limit: int) -> list[dict[str, Any]]:
        """Ultimas execucoes do workflow, de qualquer status, sem o payload."""
        pagina = await self._get(
            "/api/v1/executions", {"workflowId": workflow_id, "limit": limit, "includeData": "false"}
        )
        return list(pagina.get("data") or [])

    async def get_execution(self, execution_id: int | str, include_data: bool = True) -> dict:
        return await self._get(
            f"/api/v1/executions/{execution_id}",
            {"includeData": str(include_data).lower()},
        )