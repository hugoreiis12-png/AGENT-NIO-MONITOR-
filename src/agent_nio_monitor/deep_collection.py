"""Coleta profunda (ADR D39-D42): caminho no a no do n8n, logs dos containers, acoes do RPA
e o investigador (Qwen com ferramentas) que apura a causa antes do envio.

Monta tudo sobre os clientes que o `Agent` ja tem -- nenhuma conexao nova.
Fonte desligada (sem Portainer, sem Power Automate) deixa o leitor dela em None.
"""

from __future__ import annotations

from .config.schema import AppConfig
from .investigation_tools import InvestigationTools
from .llm.client import LlmClient
from .llm.investigator import Investigator
from .sources.n8n.client import N8NClient
from .sources.n8n.execution_paths import ExecutionPathCollector
from .sources.portainer.client import PortainerClient
from .sources.portainer.logs import ContainerLogCollector
from .sources.powerautomate.action_logs import LeitorDeAcoes
from .sources.powerautomate.desktop import DesktopFlowCollector
from .sources.powerautomate.flows import DesktopFlowCatalog
from .store.repository import Repository


class DeepCollection:
    def __init__(
        self, cfg: AppConfig, repo: Repository, n8n: N8NClient,
        portainer: PortainerClient | None, desktop: DesktopFlowCollector | None, llm: LlmClient,
    ) -> None:
        self.caminhos = ExecutionPathCollector(n8n, repo)
        self.logs = ContainerLogCollector(portainer, repo, cfg.policy.logs) if portainer else None
        self.acoes = LeitorDeAcoes(desktop.client) if desktop else None
        self.fluxos = DesktopFlowCatalog(desktop.client, repo) if desktop else None
        ferramentas = InvestigationTools(n8n=n8n, portainer=portainer, dataverse=desktop.client if desktop else None)
        self.investigador = Investigator(
            llm, ferramentas, max_passos=cfg.llm.investigacao_max_passos, prazo=cfg.llm.investigacao_prazo_segundos,
        ) if cfg.llm.investigacao else None
