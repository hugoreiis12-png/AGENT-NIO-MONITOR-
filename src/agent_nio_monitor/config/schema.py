"""Contrato de configuracao. Validado com Pydantic v2 (o doc original usava
`@validator`, que foi removido no v2, em um projeto que declara v2)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceConfig(BaseModel):
    """Um vendor. n8n e Portainer tem URLs, chaves e TLS proprios."""

    url: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    verify_ssl: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)


class PowerAutomateConfig(BaseModel):
    """Dataverse de um ambiente da Power Platform, lido pelo service principal.

    Credencial por client credentials (Entra ID): so os NOMES das variaveis ficam aqui.
    """

    dataverse_url: str = Field(min_length=1, pattern=r"^https://")
    tenant_id_env: str = "POWERAUTOMATE_TENANT_ID"
    client_id_env: str = "POWERAUTOMATE_CLIENT_ID"
    client_secret_env: str = "POWERAUTOMATE_CLIENT_SECRET"
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)


class BubbleConfig(BaseModel):
    """App Bubble (comprover): Data API + health de workflows expostos.

    Token por env var (BUBBLE_API_TOKEN); os 9 error types sao configuraveis.
    """

    api_root: str = Field(min_length=1, pattern=r"^https://")
    token_env: str = "BUBBLE_API_TOKEN"
    verify_ssl: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    error_types: list[str] = Field(default_factory=list)
    workflow_check: bool = True


class EnvironmentConfig(BaseModel):
    n8n: SourceConfig
    portainer: SourceConfig | None = None
    powerautomate: PowerAutomateConfig | None = None
    bubble: BubbleConfig | None = None


class PollingConfig(BaseModel):
    """Coleta entre 25 s e 1 min; fila entre 5 e 25 s (decisao do dono).

    O piso da coleta protege as fontes (`GET /api/v1/workflows` devolve 11,2 MB: a 60 s
    ja sao ~16 GB/dia). A fila nao le as fontes (so estado local e 1 consulta por
    execucao pendente), entao gira mais rapido: espera + tick <= `queue_max_delay_seconds`.
    """

    execution_errors_seconds: float = Field(default=30.0, ge=25.0, le=60.0)
    workflow_full_seconds: float = Field(default=60.0, ge=25.0, le=60.0)
    container_state_seconds: float = Field(default=30.0, ge=25.0, le=60.0)
    container_stats_seconds: float = Field(default=60.0, ge=25.0, le=60.0)
    stack_changes_seconds: float = Field(default=60.0, ge=25.0, le=60.0)
    bubble_poll_seconds: float = Field(default=30.0, ge=25.0, le=60.0)
    queue_seconds: float = Field(default=10.0, ge=5.0, le=25.0)
    queue_max_delay_seconds: float = Field(default=50.0, ge=30.0, le=120.0)

    # `forbid`: reconciliation_seconds, digest_hour e health_seconds eram aceitos e nunca lidos.
    model_config = ConfigDict(extra="forbid")


class SmtpConfig(BaseModel):
    """SMTP direto (D46): so entra quando o webhook do n8n esgota. Senha so por variavel."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1)
    port: int = Field(default=587, ge=1, le=65535)
    seguranca: Literal["starttls", "ssl", "nenhuma"] = "starttls"
    from_address: str = Field(min_length=3)
    username_env: str = "SMTP_USERNAME"
    password_env: str = "SMTP_PASSWORD"
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)


class MailConfig(BaseModel):
    """Regra de canal (ADR do plano): n8n no caminho normal, log no n8n morto."""

    webhook_url: str
    webhook_path: str = "/webhook/agent-nio-monitor"
    # O webhook so responde depois que o SMTP envia (responseMode lastNode): 15 s nao bastava.
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_attempts: int = Field(default=3, ge=1, le=10)
    # Imagens do e-mail HTML (logo e herois) ficam hospedadas; sem isso o e-mail
    # ainda mostra todo o texto, so perde a arte.
    assets_base_url: str = ""
    support_email: str = ""
    smtp: SmtpConfig | None = None


class LlmConfig(BaseModel):
    """O LLM analisa e REDIGE. Nao decide severidade nem destinatario (ADR D3)."""

    base_url: str
    model: str
    timeout_seconds: float = Field(default=60.0, gt=0)
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    max_tokens: int = Field(default=700, ge=64)
    # Se o LLM cair, o alerta precisa sair assim mesmo -- com texto deterministico.
    fallback_to_template: bool = True
    # Investigacao com ferramentas (tool calling no formato Hermes do vLLM) antes de redigir (ADR D42).
    investigacao: bool = True
    investigacao_max_passos: int = Field(default=4, ge=1, le=8)
    investigacao_prazo_segundos: float = Field(default=75.0, ge=10.0, le=150.0)


class DatabaseConfig(BaseModel):
    dsn: str = Field(min_length=1)
    # `schema` colidiria com BaseModel.schema() e emite warning; o YAML mantem
    # a palavra `schema` via alias.
    db_schema: str = Field(
        default="hermes", alias="schema", pattern=r"^[a-z_][a-z0-9_]*$"
    )
    pool_min: int = Field(default=1, ge=1)
    pool_max: int = Field(default=8, ge=1)
    # Dias de historico mantidos; None = nao apaga nada. Piso de 60 preserva o mes anterior inteiro.
    retention_days: int | None = Field(default=None, ge=60)

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def _pool_coerente(self) -> DatabaseConfig:
        if self.pool_min > self.pool_max:
            raise ValueError("pool_min nao pode ser maior que pool_max")
        return self


class NotificationRule(BaseModel):
    """Motor de regras unico. O doc original tinha DUAS fontes de verdade
    (notification_rules.yaml + RiskAnalyzer hardcoded com `score += 4`), e
    config que mente em silencio e como isso acontece."""

    name: str
    event_types: list[str] = Field(min_length=1)
    severity: Literal["critical", "high", "medium", "low", "info"] = "medium"
    min_streak: int = Field(default=1, ge=1)
    notify: bool = True
    escalate_after_minutes: list[int] = Field(default_factory=list)
    template: str = "generic"
    # None = herda o global. Existe porque um critico JA escalado nao deve
    # repetir a cada 4h: ele tem escada de escalacao, nao repeticao.
    re_notify_after_minutes: int | None = Field(default=None, ge=0)


class AggregationConfig(BaseModel):
    """D4: evento util e 'X falhou N vezes desde T', nao 'execucao Y falhou'.

    Medido: 24 erros/dia, com 3 workflows = 68% deles. Deduplicar por workflow
    e o que separa 24 emails/dia de 3-5.
    """

    # `forbid`: chave sem leitor no codigo e config que mente (window_minutes e min_occurrences eram).
    model_config = ConfigDict(extra="forbid")

    re_notify_after_minutes: int = Field(default=240, ge=0)
    # Alteracao (workflow, credencial, stack) so vira alerta depois de N min sem nova edicao.
    edicao_quieta_minutos: int = Field(default=10, ge=0, le=120)


class QuietHoursConfig(BaseModel):
    enabled: bool = False
    timezone: str = "America/Sao_Paulo"
    start: str = Field(default="22:00", pattern=r"^\d{2}:\d{2}$")
    end: str = Field(default="08:00", pattern=r"^\d{2}:\d{2}$")
    critical_breaks_quiet_hours: bool = True


class ThresholdsConfig(BaseModel):
    """O que torna uma mudanca ou consumo RELEVANTE o bastante para alertar."""

    stack_change_ratio: float = Field(default=0.20, gt=0, le=1)
    restart_loop_count: int = Field(default=3, ge=2)
    restart_loop_window_minutes: int = Field(default=10, ge=1)
    cpu_host_pct: float = Field(default=80.0, gt=0, le=100)
    mem_pct: float = Field(default=90.0, gt=0, le=100)
    # Score de risco do diff (0-10) ate o qual a alteracao de workflow e refactor (alerta verde).
    refactor_max_score: int = Field(default=1, ge=0, le=10)
    sustained_samples: int = Field(default=3, ge=1)


class LogsConfig(BaseModel):
    """Leitura dos logs dos containers (ADR D40): o que e erro novo, o que e pico e o que se ignora."""

    model_config = ConfigDict(extra="forbid")

    max_containers_por_ciclo: int = Field(default=12, ge=1, le=100)
    pico_minimo: int = Field(default=20, ge=1)
    fator_de_pico: float = Field(default=5.0, ge=1.0)
    container_n8n: str = "n8n"
    # Container (ou "*") -> regex de linhas que nao sao erro de verdade.
    ignorar: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _regex_validas(self) -> LogsConfig:
        for padroes in self.ignorar.values():
            for padrao in padroes:
                re.compile(padrao)
        return self


class CorrelacaoWorkflow(BaseModel):
    """Workflow do n8n do qual a automacao depende. Nome humano + id real:
    o par e validado no boot (used pelo `Correlacionador`, que consulta execucoes por id)."""

    name: str = Field(min_length=1)
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")


class CorrelacaoConfig(BaseModel):
    """Ponte declarada entre uma automacao externa e os workflows do n8n dela.

    O caso que fechou a feature (28/10/2026): o pipeline mcd-donalds pagou a
    espera de 270 s do reset_link e fracassou, o `MB Event Broker` respondia
    VERDE (execucoes success) -- a quebra estava na dependencia indireta, e
    nenhuma fonte isolada mostrava o nexo. Aqui se declara: nomes que casam a
    automacao (regex, case-insensitive, sobre nome de container/pipeline),
    marcadores de texto que delatam a espera e os workflows a consultar.
    """

    model_config = ConfigDict(extra="forbid")

    automacao: list[str] = Field(min_length=1)
    marcadores: list[str] = Field(min_length=1)
    workflows: list[CorrelacaoWorkflow] = Field(min_length=1)
    # ±minutos ao redor do momento do erro. NAO e cadencia esperada: o poller
    # roda em rajadas, entao sanidade se deduz do que EXECUTOU na janela,
    # nunca da frequencia.
    janela_min: int = Field(default=15, ge=1, le=120)

    @model_validator(mode="after")
    def _regex_validas(self) -> CorrelacaoConfig:
        import re

        for padrao in (*self.automacao, *self.marcadores):
            try:
                re.compile(padrao, re.IGNORECASE)
            except re.error:
                raise ValueError(f"regex invalida: {padrao}") from None
        return self


class PolicyConfig(BaseModel):
    thresholds: ThresholdsConfig = ThresholdsConfig()
    logs: LogsConfig = LogsConfig()
    aggregation: AggregationConfig = AggregationConfig()
    quiet_hours: QuietHoursConfig = QuietHoursConfig()
    rules: list[NotificationRule] = Field(min_length=1)
    max_recipients: int = Field(default=10, ge=1, le=50)
    # vazia = feature de correlacao desligada (sem entradas, nada e consultado).
    correlacoes: list[CorrelacaoConfig] = Field(default_factory=list)


class AppConfig(BaseModel):
    environments: dict[str, EnvironmentConfig]
    polling: PollingConfig = PollingConfig()
    mail: MailConfig
    llm: LlmConfig
    database: DatabaseConfig
    policy: PolicyConfig
    active_environment: str = "production"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _ambiente_ativo_existe(self) -> AppConfig:
        if self.active_environment not in self.environments:
            raise ValueError(
                f"active_environment='{self.active_environment}' nao existe em environments"
            )
        return self

    @property
    def active(self) -> EnvironmentConfig:
        return self.environments[self.active_environment]
