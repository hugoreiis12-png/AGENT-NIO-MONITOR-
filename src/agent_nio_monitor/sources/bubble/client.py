"""Cliente HTTP da Data API do Bubble.

Fatos verificados (investigacao 30/10/2026):
  - Base: https://<app>.bubbleapps.io/api/1.1
  - Auth: header Authorization (apenas para /wf/; /obj/ e /meta sao publicos)
  - /meta: lista todos os tipos e workflows expostos (857 KB, sem auth)
  - /obj/<type>?cursor=N&limit=M: pagina de registros
  - /wf/<name>: POST para workflow exposto
  - NAO existe endpoint de log nativo do Bubble; a unica fonte de "log"
    sao os data types criados pelo app para registrar erros

  Cursor do Bubble: valor inteiro crescente (nula na primeira chamada).
  Diferente do cursor paginado do n8n (string) e do Portainer (JSON).
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

log = structlog.get_logger(__name__)


class BubbleApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _deve_retryar(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    status = getattr(exc, "status", None)
    if status is None and isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    if isinstance(status, int):
        return status >= 500 or status == 429
    return False


class BubbleClient:
    def __init__(
        self,
        api_root: str,
        token: str,
        *,
        timeout: float = 30.0,
        verify_ssl: bool = True,
    ) -> None:
        self.api_root = api_root.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={"Authorization": token, "Accept": "application/json"},
            timeout=timeout,
            verify=verify_ssl,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> BubbleClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @property
    def alive(self) -> bool:
        return not self._client.is_closed

    @retry(
        retry=retry_if_exception(_deve_retryar),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
        reraise=True,
    )
    async def _get(self, path: str, params: dict | None = None) -> Any:
        return (await self._fetch(path, params)).json()

    @retry(
        retry=retry_if_exception(_deve_retryar),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
        reraise=True,
    )
    async def _post(self, path: str, json: dict | None = None) -> Any:
        resp = await self._post_raw(path, json)
        return resp.json()

    async def _fetch(self, path: str, params: dict | None = None) -> httpx.Response:
        try:
            resp = await self._client.get(f"{self.api_root}{path}", params=params)
        except httpx.HTTPError as exc:
            raise BubbleApiError(f"erro de rede em {path}: {type(exc).__name__}") from exc
        if resp.is_error:
            raise BubbleApiError(f"{resp.status_code} em {path}", resp.status_code)
        return resp

    async def _post_raw(self, path: str, json: dict | None = None) -> httpx.Response:
        try:
            resp = await self._client.post(f"{self.api_root}{path}", json=json)
        except httpx.HTTPError as exc:
            raise BubbleApiError(f"erro de rede em {path}: {type(exc).__name__}") from exc
        if resp.is_error:
            raise BubbleApiError(f"{resp.status_code} em {path}", resp.status_code)
        return resp

    # ------------------------------------------------------------- endpoints

    async def fetch_meta(self) -> dict[str, Any]:
        """Contrato de design completo: tipos, workflows GET/POST, app_data."""
        return await self._get("/meta")

    async def fetch_type_page(
        self, type_name: str, *, cursor: int = 0, limit: int = 20,
        sort_field: str = "Created Date", descending: bool = True,
    ) -> dict[str, Any]:
        """Pagina de registros de um data type, ordenado por data de criacao (mais recente primeiro).

        `cursor` e o valor retornado pelo Bubble no campo `cursor` da resposta anterior.
        Na primeira chamada, cursor=0 traz a pagina mais recente.
        """
        params: dict[str, Any] = {
            "cursor": cursor,
            "limit": limit,
            "sort_field": sort_field,
            "descending": "true" if descending else "false",
            "exclude_remaining": "true",
        }
        return await self._get(f"/obj/{type_name}", params)

    async def check_workflow(self, name: str, params: dict | None = None) -> tuple[int, str | None]:
        """Dispara um workflow com body vazio para testar sua disponibilidade.

        Devolve (status_code, mensagem_de_erro). 200 = ok; 4xx = falha esperada (auth/validacao);
        5xx = erro interno do Bubble (incidente).
        """
        try:
            resp = await self._client.post(f"{self.api_root}/wf/{name}", json=params or {})
        except httpx.HTTPError as exc:
            raise BubbleApiError(f"erro de rede em /wf/{name}: {type(exc).__name__}") from exc
        if resp.status_code < 500:
            return resp.status_code, None
        try:
            body = resp.json()
            msg = body.get("message") or body.get("error") or ""
        except Exception:
            msg = resp.text[:200]
        return resp.status_code, str(msg)
