"""Rotacao de credencial no n8n.

Regra (`credencial_alterada`) e template (`notify/templates.py`) existiam ha
anos sem nenhum emissor -- um dos nomes mortos que a auditoria da Fase 1
encontrou.

Fonte: `GET /api/v1/credentials`, verificado na instancia real -- HTTP 200,
39 credenciais, campos `id`, `name`, `type`, `createdAt`, `updatedAt` e
`shared`. Duas consequencias de projeto:

**`shared` nao e guardado.** Traz `Endereco nio@grupocampanha.com.br` com papel
de acesso -- e-mail de pessoa dentro do banco e dentro do alerta. O coletor
descarta o campo antes de gravar; o alerta carrega so a QUANTIDADE de quem
tem acesso, nunca QUEM.

**Nao existe campo de autoria.** A API nao diz QUEM alterou a credencial, so
QUEM tem acesso. O alerta portanto fala do que mudou e quando, e nao de quem
mudou -- e o texto do template diz isso, para o alerta nao prometer o que nao
sabe.

Estado anterior: a tabela `resource` com `kind='credential'` ja era prevista
no schema (migration 001) e nunca foi usada. `meta` guarda `type` e
`updatedAt`; nenhuma migration nova.
"""

from __future__ import annotations

from typing import Any

import structlog

from ...store.repository import EventRecord, Repository, new_event_id, now_utc
from ..base import CollectorResult, ainda_em_edicao, instante
from .client import N8NApiError, N8NClient

log = structlog.get_logger(__name__)

KIND = "credential"


class CredentialCollector:
    """Compara as credenciais de agora com as do ultimo ciclo.

    Tres situacoes, uma so emite:
      - **nova**: `id` que o agente nunca viu -> alerta
      - **alterada**: mesmo `id`, `updatedAt` diferente -> alerta
      - **inalterada**: `updatedAt` igual -> silencio

    A primeira coleta completa APENAS forma a linha de base e nao avisa: sem
    historico, "39 credenciais" nao e novidade, e 39 alertas de uma vez seria
    o pior arranque possivel. Mesma regra do `WorkflowMonitor`.
    """

    source = "n8n"

    def __init__(self, client: N8NClient, repo: Repository, *, quieto_minutos: int = 0) -> None:
        self.client = client
        self.repo = repo
        self.quieto_minutos = quieto_minutos

    async def check_credentials(self) -> CollectorResult:
        conhecidos = {r["external_id"]: r for r in self.repo.list_resources(self.source, KIND)}
        try:
            atuais = [c async for c in self.client.iter_credentials()]
        except N8NApiError as exc:
            log.error("listagem_de_credenciais_falhou", erro=str(exc))
            return CollectorResult(error=str(exc))

        # Baseline na primeira passada. `resource` ja tinha `kind='credential'`
        # reservado no schema, entao um ambiente antigo pode ter linhas de
        # outra ferramenta -- so o que NAO tem `meta.updated_at` conta como
        # estado desconhecido.
        if not conhecidos:
            log.info(
                "credenciais_linha_de_base_formada",
                total=len(atuais),
                efeito="esta coleta nao avisa; a proxima avisa",
            )
            self._gravar(atuais)
            return CollectorResult(
                collected=len(atuais), extra={"eventos": []}
            )

        # Credencial ainda sendo editada: nem alerta nem grava -- o proximo ciclo compara com a base antiga.
        prontas = [c for c in atuais if not self._em_edicao(c, conhecidos)]
        eventos = [e for e in (self._comparar(prontas, conhecidos).values()) if e is not None]
        self._gravar(prontas)
        return CollectorResult(collected=len(atuais), extra={"eventos": eventos})

    def _em_edicao(self, cred: dict[str, Any], conhecidos: dict[str, dict[str, Any]]) -> bool:
        anterior = (conhecidos.get(str(cred.get("id") or "")) or {}).get("meta") or {}
        mudou = str(cred.get("updatedAt") or "") != str(anterior.get("updated_at") or "")
        return mudou and ainda_em_edicao(instante(cred.get("updatedAt")), self.quieto_minutos, now_utc())

    def _comparar(
        self, atuais: list[dict[str, Any]], conhecidos: dict[str, dict[str, Any]]
    ) -> dict[str, EventRecord | None]:
        """Um evento por credencial alterada, indexado por id (o ultimo vence).

        Indexar por id evita duplicata quando a mesma credencial aparece
        repetida na paginacao do n8n -- o que acontece e produziria dois
        alertas identicos.
        """
        saida: dict[str, EventRecord | None] = {}
        for cred in atuais:
            cid = str(cred.get("id") or "")
            if not cid:
                continue
            anterior = conhecidos.get(cid)
            marca = str(cred.get("updatedAt") or "")
            if anterior is None:
                saida[cid] = self._evento(cred, "nova", None)
            elif marca and marca != str((anterior.get("meta") or {}).get("updated_at") or ""):
                saida[cid] = self._evento(cred, "alterada", (anterior.get("meta") or {}).get("updated_at"))
        return saida

    def _evento(
        self, cred: dict[str, Any], tipo: str, anterior: str | None
    ) -> EventRecord:
        nome = str(cred.get("name") or "")
        tipo_cred = str(cred.get("type") or "")
        compartilhada = len(cred.get("shared") or [])
        quando = str(cred.get("updatedAt") or "")
        acao = (
            f"A credencial {nome} ({tipo_cred}) foi criada."
            if tipo == "nova"
            else f"A credencial {nome} ({tipo_cred}) foi alterada."
        )
        return EventRecord(
            id=new_event_id(),
            ts=now_utc(),
            source=self.source,
            event_type="credential_updated",
            severity="high",
            title=f"Credencial alterada: {nome}",
            resource_id=str(cred.get("id") or ""),
            detail={
                # `workflow_nome` e a chave que o template usa para {nome}.
                "workflow_nome": nome,
                "recurso": nome,
                "detalhe": (
                    f"{acao} Em {quando or 'data desconhecida'}. "
                    f"{compartilhada} pessoa(s) com acesso. "
                    "A API do n8n nao informa QUEM alterou -- so quem tem acesso."
                ),
                "credencial_id": str(cred.get("id") or ""),
                "credencial_nome": nome,
                "credencial_tipo": tipo_cred,
                "tipo_mudanca": tipo,
                "atualizada_em": quando,
                "atualizada_anteriormente": anterior,
                # Contagem, nunca a lista. `shared` traz e-mail de pessoa.
                "compartilhada_com": compartilhada,
                "template": "credential_updated",
            },
        )

    def _gravar(self, atuais: list[dict[str, Any]]) -> None:
        """Grava o estado para a proxima comparacao.

        `meta` leva SO `type` e `updated_at`. O `shared` e descartado aqui: e o
        unico lugar do projeto em que dado de pessoa entraria no banco por
        esse caminho, e nao ha motivo para guarda-lo.
        """
        for cred in atuais:
            cid = str(cred.get("id") or "")
            if not cid:
                continue
            self.repo.upsert_resource(
                source=self.source,
                kind=KIND,
                external_id=cid,
                name=str(cred.get("name") or ""),
                critical=True,  # credencial e sempre sensivel
                meta={
                    "type": str(cred.get("type") or ""),
                    "updated_at": str(cred.get("updatedAt") or ""),
                },
            )