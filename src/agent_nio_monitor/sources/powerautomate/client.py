"""Cliente do Dataverse (Web API) autenticado pelo service principal.

Client credentials no Entra ID com escopo `<dataverse_url>/.default`. Nao ha API
permission no Entra para isto: o acesso vem do *usuario de aplicativo* cadastrado no
ambiente, com a funcao de seguranca `NIO Monitor Leitura`.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ...redaction import redact_text

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
API_PATH = "/api/data/v9.2/"
MARGEM_DO_TOKEN_SEGUNDOS = 120
PREFER_ROTULOS = 'odata.include-annotations="OData.Community.Display.V1.FormattedValue"'
ODATA_HEADERS = {"Accept": "application/json", "OData-MaxVersion": "4.0", "OData-Version": "4.0"}

# Os AADSTS que ja apareceram neste tenant (runbook do NIO-CLI) e o que fazer com cada um.
DIAGNOSTICO_AADSTS = {
    "AADSTS7000215": "secret invalido: foi colado o Secret ID em vez do Value",
    "AADSTS7000222": "secret EXPIRADO: gere outro no Entra e atualize o .env",
    "AADSTS700016": "client id nao existe neste tenant: confira os dois GUIDs",
    "AADSTS90002": "tenant nao encontrado: confira o tenant id",
}


class DataverseError(Exception):
    def __init__(self, mensagem: str, status: int | None = None) -> None:
        super().__init__(mensagem)
        self.status = status


def _deve_retryar(exc: BaseException) -> bool:
    """Mesma politica dos outros clientes: rede, 5xx e 429 sao transitorios; 4xx nao."""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    status = getattr(exc, "status", None)
    return isinstance(status, int) and (status >= 500 or status == 429)


def _mensagem_do_erro(resp: httpx.Response) -> str:
    """Texto do erro do Dataverse/Entra, redigido e curto. Nunca o corpo inteiro."""
    try:
        corpo = resp.json()
    except ValueError:
        return redact_text(resp.text[:300])
    erro = corpo.get("error")
    if isinstance(erro, dict):
        mensagem = str(erro.get("message", ""))
        # O 403 do Dataverse diz o privilegio no FIM de ~500 caracteres de ids; e so ele que importa.
        privilegio = re.search(r"prv\w+", mensagem)
        if privilegio:
            return f"falta o privilegio {privilegio.group(0)}"
        return redact_text(mensagem[:300])
    return redact_text(str(corpo.get("error_description") or erro or "")[:300])


class DataverseClient:
    def __init__(self, base_url: str, *, tenant_id: str, client_id: str, client_secret: str,
                 timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._credencial = {"tenant": tenant_id, "client_id": client_id, "client_secret": client_secret}
        self._client = httpx.AsyncClient(timeout=timeout, headers=ODATA_HEADERS)
        self._token: str | None = None
        self._expira_em = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _obter_token(self) -> str:
        if self._token and time.monotonic() < self._expira_em:
            return self._token
        resp = await self._client.post(
            TOKEN_URL.format(tenant=self._credencial["tenant"]),
            data={
                "grant_type": "client_credentials",
                "client_id": self._credencial["client_id"],
                "client_secret": self._credencial["client_secret"],
                "scope": f"{self.base_url}/.default",
            },
        )
        if resp.status_code != 200:
            # Sem status: recusa do Entra nao e o 401 do Dataverse (usuario de aplicativo ausente).
            raise DataverseError(_diagnostico_de_token(resp))
        dados = resp.json()
        self._token = dados["access_token"]
        self._expira_em = time.monotonic() + int(dados.get("expires_in", 3600)) - MARGEM_DO_TOKEN_SEGUNDOS
        return self._token

    @retry(
        retry=retry_if_exception(_deve_retryar),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
        reraise=True,
    )
    async def get(self, recurso: str, params: dict[str, Any] | None = None, *,
                  anotacoes: bool = False) -> dict[str, Any]:
        """`anotacoes`: devolve tambem o rotulo legivel de lookup e opcao (nome do fluxo, maquina)."""
        headers = {"Authorization": f"Bearer {await self._obter_token()}"}
        if anotacoes:
            headers["Prefer"] = PREFER_ROTULOS
        resp = await self._client.get(f"{self.base_url}{API_PATH}{recurso}", params=params, headers=headers)
        if resp.status_code != 200:
            raise DataverseError(f"HTTP {resp.status_code}: {_mensagem_do_erro(resp)}", resp.status_code)
        return resp.json()

    @retry(
        retry=retry_if_exception(_deve_retryar),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
        reraise=True,
    )
    async def arquivo(self, recurso: str, coluna: str) -> bytes | None:
        """Conteudo de uma coluna de arquivo (ex.: o log de acoes da sessao). None quando nao ha arquivo (404)."""
        headers = {"Authorization": f"Bearer {await self._obter_token()}"}
        resp = await self._client.get(f"{self.base_url}{API_PATH}{recurso}/{coluna}/$value", headers=headers)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise DataverseError(f"HTTP {resp.status_code}: {_mensagem_do_erro(resp)}", resp.status_code)
        return resp.content


def _diagnostico_de_token(resp: httpx.Response) -> str:
    mensagem = _mensagem_do_erro(resp)
    codigo = re.search(r"AADSTS\d+", mensagem)
    if codigo and codigo.group(0) in DIAGNOSTICO_AADSTS:
        return f"{codigo.group(0)}: {DIAGNOSTICO_AADSTS[codigo.group(0)]}"
    return f"token recusado pelo Entra ID (HTTP {resp.status_code}): {mensagem}"
