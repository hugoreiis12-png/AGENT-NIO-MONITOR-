"""Resolucao de destinatarios.

Definicao do dono do projeto: responsavel no **n8n** (project) OU no
**Portainer** (ResourceControl da stack), uniao dos dois.

Ordem de resolucao, com degradacao explicita:
  1. n8n `/projects/{id}/users` + `/users`  (so se a key for owner/admin)
  2. `people.yaml`                            (fallback declarado)
  3. global de seguranca                      (ninguem em silencio e o pior cenario)

A distincao importa: em 2 e 3 o log tem que dizer "fallback", porque "ninguem
foi notificado" e o modo de falha que faz um monitor virar furniture.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
CAMPOS_DE_PESSOA = ("autor", "responsavel_stack")


@dataclass
class Resolution:
    emails: list[str] = field(default_factory=list)
    source: str = "nenhum"
    detail: str = ""


class RecipientResolver:
    def __init__(
        self,
        n8n_client,  # noqa: ANN001
        people: dict[str, list[str]],
        *,
        usuarios: dict[str, str] | None = None,
        exclusivos: dict[str, list[str]] | None = None,
        global_fallback: list[str] | None = None,
        max_recipients: int = 10,
    ) -> None:
        self.client = n8n_client
        self.people = people or {}
        self.exclusivos = exclusivos or {}
        self.usuarios = {k.lower(): v for k, v in (usuarios or {}).items()}
        self.global_fallback = global_fallback or []
        self.max_recipients = max_recipients
        self._user_by_id: dict[str, str] | None = None
        self._cache: dict[str, Resolution] = {}

    async def _load_users(self) -> dict[str, str] | None:
        """Mapa id -> email. None quando a key nao tem permissao de owner."""
        if self._user_by_id is not None:
            return self._user_by_id
        try:
            users = await self.client.list_users()
        except Exception as exc:  # noqa: BLE001 - 403 aqui e esperado, nao e bug
            log.warning("lista_de_usuarios_indisponivel_usando_fallback", erro=str(exc))
            self._user_by_id = None
            return None
        self._user_by_id = {str(u.get("id")): str(u.get("email") or "") for u in users}
        return self._user_by_id

    async def resolve_for_project(self, project_id: str | None) -> Resolution:
        if not project_id:
            return self._fallback(None, "sem_project_id")

        if project_id in self._cache:
            return self._cache[project_id]

        users_by_id = await self._load_users()
        emails: list[str] = []

        if users_by_id:
            try:
                membros = await self.client.list_project_users(project_id)
                for m in membros:
                    uid = str(m.get("userId") or m.get("id") or "")
                    email = users_by_id.get(uid)
                    if email:
                        emails.append(email)
            except Exception as exc:  # noqa: BLE001
                log.warning("membros_do_project_indisponiveis", project_id=project_id, erro=str(exc))

        if emails:
            r = Resolution(emails=self._dedup(emails), source="n8n_project",
                           detail=f"projectId={project_id}")
        else:
            r = self._fallback(project_id, "api_sem_permissao_ou_project_vazio")

        self._cache[project_id] = r
        return r

    def resolve_pessoas(self, nomes: list[Any]) -> Resolution:
        """'Quem mexeu' / dono da stack: e-mail embutido no texto ("Nome <e-mail>")
        ou usuario do Portainer mapeado em `people.yaml: usuarios`."""
        emails: list[str] = []
        for nome in nomes:
            texto = str(nome or "").strip()
            achado = EMAIL_RE.search(texto)
            if achado:
                emails.append(achado.group(0))
            elif texto.lower() in self.usuarios:
                emails.append(self.usuarios[texto.lower()])
            elif texto and texto.lower() != "import":
                log.warning("pessoa_sem_email_mapeado", pessoa=texto)
        return Resolution(emails=self._dedup(emails), source="autoria") if emails else Resolution()

    async def resolve_for_event(self, project_id: str | None, detail: dict[str, Any]) -> Resolution:
        """Responsavel do projeto/stack + quem mexeu no recurso. Workflow com
        lista exclusiva em `people.yaml: workflows` ignora projeto e autor."""
        exclusivo = self.exclusivos.get(str(detail.get("workflow_id")))
        if exclusivo:
            return Resolution(emails=self._dedup(exclusivo), source="workflow_exclusivo")
        base = await self.resolve_for_project(project_id)
        pessoas = self.resolve_pessoas([detail.get(c) for c in CAMPOS_DE_PESSOA])
        if not pessoas.emails:
            return base
        fontes = [x for x in (base, pessoas) if x.emails]
        return Resolution(
            emails=self._dedup([e for x in fontes for e in x.emails]),
            source="+".join(x.source for x in fontes),
            detail=base.detail,
        )

    async def resolve_for_portainer_resource(
        self, user_ids: list[str], tenant_hint: str | None
    ) -> Resolution:
        """ResourceControl do Portainer vem com id de usuario. Mesmo mapa."""
        users_by_id = await self._load_users()
        emails = [users_by_id[u] for u in user_ids if users_by_id and u in users_by_id]
        if emails:
            return Resolution(emails=self._dedup(emails), source="portainer_resource_control",
                              detail=f"hint={tenant_hint}")
        return self._fallback(tenant_hint, "portainer_sem_mapa_de_usuarios")

    def _fallback(self, tenant_id: str | None, why: str) -> Resolution:
        if tenant_id and tenant_id in self.people:
            return Resolution(
                emails=self._dedup(self.people[tenant_id]),
                source="people_yaml",
                detail=f"{why}; projectId={tenant_id}",
            )
        if self.global_fallback:
            return Resolution(
                emails=self._dedup(self.global_fallback),
                source="global_fallback",
                detail=f"{why}; SEM mapa do tenant -- alerta pode nao chegar ao dono",
            )
        return Resolution(emails=[], source="nenhum",
                          detail=f"{why}; nenhum destinatario -- ALERTA PERDIDO")

    def union(self, *resolucoes: Resolution) -> Resolution:
        """Uniao n8n + Portainer, que e exatamente a definicao de 'responsavel'."""
        vistos: list[str] = []
        fontes: list[str] = []
        for r in resolucoes:
            if r.emails:
                fontes.append(r.source)
            vistos.extend(r.emails)
        if not vistos:
            return self._fallback(None, "uniao_vazia")
        return Resolution(emails=self._dedup(vistos), source="+".join(sorted(set(fontes))))

    def _dedup(self, emails: list[str]) -> list[str]:
        vistos: set[str] = set()
        saida: list[str] = []
        for e in emails:
            e = (e or "").strip().lower()
            if e and e not in vistos:
                vistos.add(e)
                saida.append(e)
            if len(saida) >= self.max_recipients:
                log.warning("limite_de_destinatarios_atingido", limite=self.max_recipients)
                break
        return saida

    def invalidate(self) -> None:
        self._cache.clear()
        self._user_by_id = None

