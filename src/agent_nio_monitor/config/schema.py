"""Contrato de configuracao. Validado com Pydantic v2 (o doc original usava
`@validator`, que foi removido no v2, em um projeto que declara v2)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceConfig(BaseModel):
    """Um vendor. n8n e Portainer tem URLs, chaves e TLS proprios."""

    url: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    verify_ssl: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)


class EnvironmentConfig(BaseModel):
    n8n: SourceConfig
    portainer: SourceConfig | None = None


class PollingConfig(BaseModel):
    """Coleta e disparo entre 25 s e 1 min (decisao do dono).

    O piso protege as fontes (`GET /api/v1/workflows` devolve 11,2 MB: a 60 s ja sao
    ~16 GB/dia) e o teto garante que o alerta chegue em ate ~1 ciclo + a fila.
    """

    execution_errors_seconds: float = Field(default=30.0, ge=25.0, le=60.0)
    workflow_full_seconds: float = Field(default=60.0, ge=25.0, le=60.0)
    container_state_seconds: float = Field(default=30.0, ge=25.0, le=60.0)
    container_stats_seconds: float = Field(default=60.0, ge=25.0, le=60.0)
    stack_changes_seconds: float = Field(default=60.0, ge=25.0, le=60.0)
    queue_seconds: float = Field(default=30.0, ge=25.0, le=60.0)
    reconciliation_seconds: float = Field(default=3600.0, gt=0)
    digest_hour: int = Field(default=9, ge=0, le=23)
    health_seconds: float = Field(default=30.0, gt=0)


class MailConfig(BaseModel):
    """Regra de canal (ADR do plano): n8n no caminho normal, log no n8n morto."""

    webhook_url: str
    webhook_path: str = "/webhook/agent-nio-monitor"
    timeout_seconds: float = Field(default=15.0, gt=0)
    max_attempts: int = Field(default=3, ge=1, le=10)
    # Imagens do e-mail HTML (logo e herois) ficam hospedadas; sem isso o e-mail
    # ainda mostra todo o texto, so perde a arte.
    assets_base_url: str = ""
    support_email: str = ""


class LlmConfig(BaseModel):
    """O LLM analisa e REDIGE. Nao decide severidade nem destinatario (ADR D3)."""

    base_url: str
    model: str
    timeout_seconds: float = Field(default=60.0, gt=0)
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    max_tokens: int = Field(default=700, ge=64)
    # Se o LLM cair, o alerta precisa sair assim mesmo -- com texto deterministico.
    fallback_to_template: bool = True


class DatabaseConfig(BaseModel):
    dsn: str = Field(min_length=1)
    # `schema` colidiria com BaseModel.schema() e emite warning; o YAML mantem
    # a palavra `schema` via alias.
    db_schema: str = Field(
        default="hermes", alias="schema", pattern=r"^[a-z_][a-z0-9_]*$"
    )
    pool_min: int = Field(default=1, ge=1)
    pool_max: int = Field(default=8, ge=1)

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

    window_minutes: int = Field(default=60, ge=1)
    min_occurrences: int = Field(default=1, ge=1)
    re_notify_after_minutes: int = Field(default=240, ge=0)


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
    sustained_samples: int = Field(default=3, ge=1)


class PolicyConfig(BaseModel):
    thresholds: ThresholdsConfig = ThresholdsConfig()
    aggregation: AggregationConfig = AggregationConfig()
    quiet_hours: QuietHoursConfig = QuietHoursConfig()
    rules: list[NotificationRule] = Field(min_length=1)
    max_recipients: int = Field(default=10, ge=1, le=50)


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
