"""Indice workflow_id -> (nome, projectId), carregado uma vez e reutilizado."""

from __future__ import annotations

import structlog

from .client import N8NClient

log = structlog.get_logger(__name__)


class WorkflowIndex:
    def __init__(self, client: N8NClient) -> None:
        self._client = client
        self._loaded = False
        self.names: dict[str, str] = {}
        self.projects: dict[str, str] = {}

    async def load(self) -> None:
        if self._loaded:
            return
        try:
            async for wf in self._client.iter_workflows():
                wid = str(wf.get("id"))
                self.names[wid] = str(wf.get("name") or wid)
                projeto = next((s.get("projectId") for s in wf.get("shared", []) if s.get("projectId")), None)
                if projeto:
                    self.projects[wid] = str(projeto)
        except Exception as exc:  # noqa: BLE001 - nome/projeto sao opcionais, coleta nao pode morrer por isso
            log.warning("nomes_de_workflow_indisponiveis", erro=str(exc))
            return
        self._loaded = True
