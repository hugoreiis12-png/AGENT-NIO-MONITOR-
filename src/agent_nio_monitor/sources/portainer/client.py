"""Cliente Portainer CE.

Fatos verificados na doc do Portainer:
  - header e `X-API-Key`, NAO `Authorization: Bearer`
  - webhook do Portainer e OUTBOUND (redeploy disparado por registry) e e
    Business Edition -- nao serve como fonte de evento. O doc original tratava
    como se fosse.
  - Docker e exposto por proxy sob `/api/endpoints/{id}/docker/...`

Custo: `/docker/containers/json` traz State/Status/Names em UMA chamada para
todos os containers do endpoint. E por isso que o estado e 60 s mesmo com
muitos containers -- em vez de list+inspect por container, que estourava o
rate limit de 30 req/min.
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


class PortainerApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _deve_retryar(exc: BaseException) -> bool:
    """Mesma politica do cliente n8n: rede e 5xx sao transitorios, 4xx nao.

    503 do Portainer durante restart de container e 429 por rate limit sao os
    casos que precisam de insistencia.
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    status = getattr(exc, "status", None)
    if status is None and isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    if isinstance(status, int):
        return status >= 500 or status == 429
    return False


class PortainerClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        verify_ssl: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
            verify=verify_ssl,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> PortainerClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @retry(
        retry=retry_if_exception(_deve_retryar),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
        reraise=True,
    )
    async def _get(self, path: str, params: dict | None = None) -> Any:
        return (await self._fetch(path, params)).json()

    async def _fetch(self, path: str, params: dict | None = None) -> httpx.Response:
        try:
            resp = await self._client.get(f"{self.base_url}{path}", params=params)
        except httpx.HTTPError as exc:
            raise PortainerApiError(f"erro de rede em {path}: {type(exc).__name__}") from exc
        if resp.status_code in (401, 403):
            raise PortainerApiError(
                f"{resp.status_code} em {path}: a API key precisa de admin", resp.status_code
            )
        if resp.is_error:
            raise PortainerApiError(f"{resp.status_code} em {path}", resp.status_code)
        return resp

    async def list_endpoints(self) -> list[dict[str, Any]]:
        return list(await self._get("/api/endpoints"))

    async def list_stacks(self) -> list[dict[str, Any]]:
        """`GET /api/stacks` traz `UpdateDate`, `UpdatedBy`, `ConfigHash` e
        `ResourceControl`. Sao sinais de mudanca mais baratos que difar o
        compose -- e sem risco de vazar `Env` com senha."""
        return list(await self._get("/api/stacks"))

    async def stack_file(self, stack_id: int | str) -> str:
        """Compose atual da stack. So buscado quando UpdateDate/UpdatedBy mudam."""
        dados = await self._get(f"/api/stacks/{stack_id}/file")
        return str(dados.get("StackFileContent") or "")

    async def list_containers(self, endpoint_id: int) -> list[dict[str, Any]]:
        """Chamada em lote: devolve todos os containers do endpoint."""
        return list(
            await self._get(f"/api/endpoints/{endpoint_id}/docker/containers/json",
                            {"all": "true"})
        )

    async def inspect_container(self, endpoint_id: int, container_id: str) -> dict[str, Any]:
        return await self._get(
            f"/api/endpoints/{endpoint_id}/docker/containers/{container_id}/json"
        )

    async def container_stats(self, endpoint_id: int, container_id: str) -> dict[str, Any]:
        """Endpoint Docker `/stats` e STREAMING. `stream=false` + 1 amostra e o
        unico jeito de nao ficar preso; dai o timeout curto."""
        return await self._get(
            f"/api/endpoints/{endpoint_id}/docker/containers/{container_id}/stats",
            {"stream": "false", "one-shot": "true"},
            )

    async def container_logs(
        self, endpoint_id: int, container_id: str, *, tail: int = 200,
        since: int | None = None, timestamps: bool = False,
    ) -> str:
        """No ciclo, so INCREMENTAL (`since`): o log inteiro de cada container a cada ciclo e absurdo."""
        params = {"stdout": "true", "stderr": "true", "tail": str(tail), "timestamps": str(timestamps).lower()}
        if since is not None:
            params["since"] = str(since)
        resp = await self._fetch(f"/api/endpoints/{endpoint_id}/docker/containers/{container_id}/logs", params)
        return decode_docker_log(resp.content)


def decode_docker_log(raw: bytes) -> str:
    """Sem TTY o Docker multiplexa: cada quadro tem 8 bytes (tipo, 0,0,0, tamanho)."""
    partes: list[bytes] = []
    pos = 0
    while pos + 8 <= len(raw) and raw[pos] in (0, 1, 2) and raw[pos + 1 : pos + 4] == bytes(3):
        tamanho = int.from_bytes(raw[pos + 4 : pos + 8], "big")
        partes.append(raw[pos + 8 : pos + 8 + tamanho])
        pos += 8 + tamanho
    texto = b"".join(partes) if partes and pos >= len(raw) else raw
    return texto.decode("utf-8", errors="replace")


def parse_container(row: dict[str, Any]) -> dict[str, Any]:
    """Achata o formato do Docker list para o que o monitor precisa.

    O endpoint `json` da lista NAO traz RestartCount -- so o inspect traz. Por
    isso `restart_count` fica None aqui e e preenchido pelo inspect sob demanda.
    Mentir sobre isso (botar 0) faria o detector de restart-loop nunca disparar.
    """
    state = (row.get("State") or "").lower()
    status = row.get("Status") or ""
    health: str | None = None
    for prefix, rotulo in (
        ("(healthy)", "healthy"),
        ("(unhealthy)", "unhealthy"),
        ("(health: starting)", "starting"),
        ("(health: none)", None),
    ):
        if prefix in status.lower():
            health = rotulo
            break
    return {
        "container_id": str(row.get("Id") or "")[:12],
        "name": (row.get("Names") or ["/?"])[0].lstrip("/"),
        "image": row.get("Image"),
        "state": state,
        "status": status,
        "health": health,
        "restart_count": None,
        "created": row.get("Created"),
    }
