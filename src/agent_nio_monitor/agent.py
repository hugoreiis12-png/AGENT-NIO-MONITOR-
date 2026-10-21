"""Pipeline principal. Cada estagio e isolado e testavel; `main.py` so monta o
CLI e o scheduler em torno desta classe.

Ordem deliberada: health sobe -> sonda de capacidade (D2) -> coleta -> regra ->
texto -> envio -> registro. O health ANTES de tudo, porque e o que o cron
externo consulta e o unico sinal que sobrevive a um boot quebrado.
"""

from __future__ import annotations

from typing import Any

import structlog

from .boot_probe import sondar_no_boot
from .config.schema import AppConfig
from .deep_collection import DeepCollection
from .health import HealthState
from .llm.analyst import AlertAnalyst
from .llm.client import LlmClient
from .notify.mailer import DispatchResult, Notifier
from .notify.pipeline import EventPipeline
from .notify.resolution import ResolutionChecker
from .notify.people import exclusive_by_source, exclusive_by_workflow, fallback_de_emergencia, people_by_project, portainer_users
from .notify.resolver import RecipientResolver
from .notify.sender import enviar_alerta
from .notify.smtp_fallback import build_smtp_fallback
from .rules.engine import RuleEngine
from .sources.n8n.collector import ExecutionErrorCollector
from .sources.n8n.credentials import CredentialCollector
from .sources.n8n.events import evento_de_buraco, evento_de_falha, evento_de_queda
from .sources.n8n.suite import build_n8n
from .sources.n8n.workflow_index import WorkflowIndex
from .sources.n8n.workflow_monitor import WorkflowMonitor
from .sources.portainer.suite import build_portainer
from .sources.powerautomate.cycle import eventos_de_erro_tratado, eventos_de_falha
from .sources.powerautomate.suite import build_power_automate
from .store.maintenance import PartitionMaintenance
from .store.queue import InMemoryQueue, NotificationQueue
from .store.repository import EventRecord, Repository, now_utc

log = structlog.get_logger()


class Agent:
    def __init__(
        self, cfg: AppConfig, repo: Repository, queue: NotificationQueue | None = None,
        maintenance: PartitionMaintenance | None = None,
    ) -> None:
        self.cfg = cfg
        self.repo = repo
        self.maintenance = maintenance
        # `cfg.policy`, e nao `load_policy()`: o `loader` ja leu e validou o
        # arquivo em `load()`. Chamar de novo fazia uma segunda leitura de disco
        # e criava um SEGUNDO objeto `PolicyConfig` -- dois objetos para a mesma
        # fonte de verdade, que divergem silenciosamente se o arquivo mudar no
        # meio do boot.
        self.policy = cfg.policy
        self.rules = RuleEngine(self.policy)
        # Estado anterior da alcancabilidade do n8n, para emitir `source_down`
        # so na TRANSICAO. `None` = ainda nao se sabe, e nao conta como queda.
        self._n8n_era_up: bool | None = None
        self.health = HealthState()
        self.llm = LlmClient(
            cfg.llm.base_url, cfg.llm.model,
            timeout=cfg.llm.timeout_seconds,
            temperature=cfg.llm.temperature,
            max_tokens=cfg.llm.max_tokens,
        )
        self.analyst = AlertAnalyst(self.llm, enabled=cfg.llm.fallback_to_template)
        self.notifier = Notifier(
            webhook_url=f"{cfg.mail.webhook_url}{cfg.mail.webhook_path}",
            timeout=cfg.mail.timeout_seconds,
            max_attempts=cfg.mail.max_attempts,
            verify_ssl=cfg.active.n8n.verify_ssl,
            fallback=build_smtp_fallback(cfg.mail.smtp),
        )
        self.resolver = RecipientResolver(
            build_n8n(cfg),
            people=people_by_project(),
            usuarios=portainer_users(),
            exclusivos=exclusive_by_workflow(),
            por_fonte=exclusive_by_source(),
            global_fallback=fallback_de_emergencia(),
            max_recipients=self.policy.max_recipients,
        )
        self.n8n = self.resolver.client
        self.index = WorkflowIndex(self.n8n)
        self.workflow_monitor = WorkflowMonitor(
            self.n8n, self.repo, refactor_max_score=self.policy.thresholds.refactor_max_score,
            quieto_minutos=self.policy.aggregation.edicao_quieta_minutos,
        )
        self.containers, self.stacks, self.resources = build_portainer(cfg, self.repo) or (None, None, None)
        self.desktop = build_power_automate(cfg, self.repo)
        portainer = self.containers.client if self.containers is not None else None
        self.deep = DeepCollection(cfg, self.repo, self.n8n, portainer, self.desktop, self.llm)
        self.analyst.investigador = self.deep.investigador
        self.pipeline = EventPipeline(
            repo=self.repo, queue=queue if queue is not None else InMemoryQueue(), rules=self.rules,
            polling=cfg.polling, health=self.health, dispatch=self.dispatch,
            checker=ResolutionChecker(
                containers=self.containers, resources=self.resources, n8n=self.n8n, repo=self.repo,
                limites=cfg.policy.thresholds, desktop=self.desktop, deep=self.deep,
            ),
        )

    # ------------------------------------------------------------ ciclo n8n

    async def collect_execution_errors(self) -> list[EventRecord]:
        collector = ExecutionErrorCollector(self.n8n, self.repo)
        await self.index.load()
        resultado = await collector.run(name_by_id=self.index.names)
        self.health.mark("n8n_executions", resultado.ok)
        self.health.mark_n8n(self.n8n.alive)
        if resultado.gap:
            self.health.conta_gap()

        # O buraco vem ANTES do early-return: uma varredura completa que nao
        # acha nenhuma execucao nova ainda assim perdeu historico, e essa e a
        # informacao que o dono precisa.
        eventos: list[EventRecord] = list(self._detecta_queda_do_n8n())
        if resultado.gap:
            eventos.append(self._evento_de_buraco(resultado))

        if not resultado.collected:
            return eventos

        # O streak ja foi acumulado pelo coletor. Aqui so se DECIDE o que fazer.
        ultimos = resultado.extra.get("ultimos_erros", {})
        for workflow_id, streak in resultado.extra.get("streaks", {}).items():
            decisao = self.rules.evaluate(
                event_type="execution_failed",
                streak_count=streak.count,
                last_notified_at=streak.last_notified_at,
                now=now_utc(),
            )
            eventos.append(evento_de_falha(workflow_id, streak, ultimos.get(workflow_id), decisao, self.index))
        return eventos

    def _evento_de_buraco(self, resultado: Any) -> EventRecord:  # noqa: ANN001 - CollectorResult
        """Buraco de reconciliacao (D7). Vai antes das falhas: explica por que pode faltar uma."""
        decisao = self.rules.evaluate(event_type="reconciliation_gap", now=now_utc())
        return evento_de_buraco(resultado.extra.get("reconciliation") or {}, decisao)

    def _detecta_queda_do_n8n(self) -> list[EventRecord]:
        """Emite `source_down` na TRANSICAO, nao a cada ciclo.

        `notify: false` na policy e estrutural: o canal de envio e o webhook do
        proprio n8n, entao o valor do evento e o RASTREIO (gravado e logado), e
        `EventPipeline.enqueue` nao o enfileira. A recuperacao so gera log.
        """
        anterior = self._n8n_era_up
        atual = self.health.n8n_up
        self._n8n_era_up = atual

        if atual is False and anterior is not False:
            decisao = self.rules.evaluate(event_type="source_down", now=now_utc())
            log.error(
                "n8n_ficou_fora",
                titulo="n8n indisponivel",
                efeito="coleta de execucoes e diff de workflow parados; "
                       "alertas por e-mail impossiveis (o webhook e do proprio n8n)",
            )
            return [evento_de_queda(decisao)]
        if atual is True and anterior is False:
            log.info("n8n_ficou_de_pe", titulo="n8n voltou a responder")
        return []

    # -------------------------------------------------------- pipeline de alerta

    async def dispatch(self, evento: EventRecord) -> DispatchResult:
        return await enviar_alerta(
            evento, resolver=self.resolver, analyst=self.analyst, notifier=self.notifier,
            repo=self.repo, health=self.health, cfg=self.cfg,
        )

    async def run_once(self) -> dict[str, Any]:
        inicio = now_utc()
        eventos = await self.collect_execution_errors()
        enfileirados = await self.process_events(eventos)
        return {
            "eventos": len(eventos),
            "enfileirados": enfileirados,
            "ms": int((now_utc() - inicio).total_seconds() * 1000),
        }

    async def process_events(self, eventos: list[EventRecord]) -> int:
        """Grava e enfileira. Quem envia e a fila (`flush_queue`), que antes reconfere
        se o problema continua -- um unico lugar para todos os coletores."""
        return self.pipeline.enqueue(eventos)

    async def flush_queue(self) -> dict[str, int]:
        return await self.pipeline.flush()

    # --------------------------------------------------------- ciclos T5/T6

    def _decidir(self, evento: EventRecord) -> EventRecord:
        """Aplica a politica unica (RuleEngine) num evento ja construido pelo
        coletor. Mesma logica que `collect_execution_errors` aplica inline --
        aqui centralizada porque workflow/container nao tem streak proprio."""
        decisao = self.rules.evaluate(event_type=evento.event_type, streak_count=evento.streak_count, now=evento.ts)
        evento.severity = decisao.severity
        evento.detail.setdefault("autor", self.index.authors.get(evento.resource_id or ""))
        evento.tenant_id = evento.tenant_id or self.index.projects.get(evento.resource_id or "")
        evento.detail["notificar"] = decisao.notify
        evento.detail["motivo_supressao"] = decisao.suppress_reason
        evento.detail["template"] = decisao.template
        return evento

    async def _ciclo(self, resultado, chave: str) -> int:  # noqa: ANN001 - CollectorResult
        self.health.mark(chave, resultado.ok)
        # Falha parcial nao vira `error` (o resto da fonte responde), mas
        # tambem nao pode sumir: era o que escondia um endpoint inteiro do
        # Portainer fora do ar enquanto o /healthz dizia "ok".
        escuros = resultado.extra.get("endpoints_escuros") or []
        if resultado.failures or escuros:
            self.health.registra_fonte_parcial(
                chave, {"falhas": resultado.failures, "endpoints_escuros": escuros}
            )
        else:
            self.health.limpa_fonte_parcial(chave)
        eventos = [self._decidir(e) for e in resultado.extra.get("eventos", [])]
        return await self.process_events(eventos)

    async def run_capabilities(self) -> dict[str, int]:
        """D2 no boot. NUNCA levanta: ver `boot_probe.sondar_no_boot`."""
        return await sondar_no_boot(self.cfg, self.repo)

    async def check_credentials(self) -> int:
        """Rotacao de credencial. Roda no mesmo ciclo do diff de workflow --
        o payload e de 39 itens, uma chamada, e ja pagamos a leitura da lista
        de workflows que e de 11,2 MB."""
        collector = CredentialCollector(self.n8n, self.repo, quieto_minutos=self.policy.aggregation.edicao_quieta_minutos)
        resultado = await collector.check_credentials()
        self.health.mark_n8n(self.n8n.alive)
        return await self._ciclo(resultado, "n8n_credentials")

    async def check_workflows_full(self) -> int:
        await self.index.load()
        resultado = await self.workflow_monitor.check_structural_diff(name_by_id=self.index.names)
        # Mesmo caminho do n8n que fala com o monitor de forma mais silenciosa:
        # `WorkflowIndex.load()` engole a excecao e so loga. Sem este espelho,
        # uma queda do n8n que so aparece aqui nunca chegaria ao `/healthz`.
        self.health.mark_n8n(self.n8n.alive)
        return await self._ciclo(resultado, "n8n_workflows_full")

    async def check_containers(self) -> int:
        if self.containers is None:
            return 0
        return await self._ciclo(await self.containers.check_state(), "portainer_containers")

    async def check_stacks(self) -> int:
        if self.stacks is None:
            return 0
        return await self._ciclo(await self.stacks.check_changes(), "portainer_stacks")

    async def check_container_resources(self) -> int:
        if self.resources is None:
            return 0
        return await self._ciclo(await self.resources.check_resources(), "portainer_resources")

    async def check_power_automate(self) -> int:
        """Execucoes de RPA: um evento por fluxo com falha neste ciclo, decidido pelo streak."""
        if self.desktop is None:
            return 0
        resultado = await self.desktop.run()
        self.health.mark("powerautomate_desktop", resultado.ok)
        fundo = await self.deep.acoes.aprofundar(resultado.extra.get("sessoes", []))
        eventos = eventos_de_falha(resultado, fundo, self.rules) + eventos_de_erro_tratado(fundo, self.deep.acoes.incidente_tratado, self._decidir)
        eventos += [self._decidir(e) for e in await self.deep.fluxos.novos()]
        return await self.process_events(eventos)

    async def check_execution_paths(self) -> int:
        """No a no das execucoes com sucesso: erro escondido e etapa do caminho normal que nao rodou."""
        await self.index.load()
        resultado = await self.deep.caminhos.run(name_by_id=self.index.names)
        self.health.mark_n8n(self.n8n.alive)
        return await self._ciclo(resultado, "n8n_execution_paths")

    async def check_container_logs(self) -> int:
        if self.deep.logs is None:
            return 0
        await self.index.load()
        por_nome = {nome: wid for wid, nome in self.index.names.items()}
        return await self._ciclo(await self.deep.logs.check_logs(por_nome), "portainer_logs")

    async def maintain_partitions(self) -> int:
        """Cria os meses a frente e aplica a retencao. Sem banco (InMemory) nao ha o que manter."""
        if self.maintenance is None:
            return 0
        return await self.maintenance.run()
