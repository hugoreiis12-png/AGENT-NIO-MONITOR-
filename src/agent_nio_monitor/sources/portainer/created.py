"""Container novo: nome que o monitor nunca viu no endpoint (recriar com o mesmo nome nao conta).

Agrupado pela stack: o deploy de uma stack nova com 3 servicos e UM aviso verde, e nao 3 mais o
da stack. Nomes conhecidos ficam em `resource` (kind `container-name`); endpoint visto pela primeira
vez (inclusive no primeiro boot) so semeia -- endpoint escuro que volta nao vira enxurrada.
"""

from __future__ import annotations

from typing import Any

from ...store.repository import EventRecord, Repository, new_event_id, now_utc
from .client import parse_container
from .donos import stack_do_container

KIND = "container-name"


class CreatedContainers:
    source = "portainer"

    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self._conhecidos: set[str] | None = None

    def _carregar(self) -> set[str]:
        if self._conhecidos is None:
            self._conhecidos = {r["external_id"] for r in self.repo.list_resources(self.source, KIND)}
        return self._conhecidos

    def novos(self, endpoint_id: int, rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """Stack (ou nome do container avulso) -> containers novos. Vazio na semente."""
        conhecidos = self._carregar()
        semente = not any(k.startswith(f"{endpoint_id}:") for k in conhecidos)
        grupos: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            container = parse_container(row)
            chave = f"{endpoint_id}:{container['name']}"
            if chave in conhecidos:
                continue
            conhecidos.add(chave)
            self.repo.upsert_resource(
                source=self.source, kind=KIND, external_id=chave, name=container["name"],
                meta={"image": container["image"]},
            )
            grupos.setdefault(stack_do_container(row) or container["name"], []).append(container)
        return {} if semente else grupos


def evento_containers_criados(
    endpoint_id: int, grupo: str, containers: list[dict[str, Any]], dono: dict,
) -> EventRecord:
    nomes = sorted(c["name"] for c in containers)
    linhas = "\n".join(f"- {c['name']} ({c['image']}, {c['state']})" for c in containers)
    return EventRecord(
        id=new_event_id(), ts=now_utc(), source="portainer", event_type="container_created", severity="low",
        title=f"{len(nomes)} container(s) criado(s) em {grupo}", resource_id=f"{endpoint_id}:{grupo}",
        detail={
            "recurso": grupo, "workflow_nome": grupo, "containers": nomes, "endpoint_id": endpoint_id,
            "detalhe": f"Container(s) novo(s) no endpoint {endpoint_id}:\n{linhas}", **dono,
        },
    )
