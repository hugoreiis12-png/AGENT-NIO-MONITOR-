"""Motor de regras.

O doc original tinha DUAS fontes de verdade para "o que e risco":
`notification_rules.yaml` E um `RiskAnalyzer` hardcoded fazendo `score += 4`.
Config que discorda do codigo e o caminho classico para config mentir em
silencio. Aqui existe SO o motor, e a config manda.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog

from ..config.schema import PolicyConfig
from ..sources.base import dentro_de_quiet_hours

log = structlog.get_logger(__name__)

_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass(frozen=True)
class Decision:
    notify: bool
    severity: str
    template: str
    suppress_reason: str | None = None
    escalate_after_minutes: tuple[int, ...] = ()


class RuleEngine:
    def __init__(self, policy: PolicyConfig) -> None:
        self.policy = policy
        # indice por event_type, preservando a ordem do arquivo
        self._by_type: dict[str, list] = {}
        for rule in policy.rules:
            for et in rule.event_types:
                self._by_type.setdefault(et, []).append(rule)

    def re_notify_minutes(self, event_type: str) -> int:
        """Janela em que o mesmo alerta nao se repete (regra do tipo, ou o padrao)."""
        regras = self._by_type.get(event_type)
        especifica = regras[0].re_notify_after_minutes if regras else None
        return especifica if especifica is not None else self.policy.aggregation.re_notify_after_minutes

    def evaluate(
        self,
        *,
        event_type: str,
        streak_count: int = 1,
        last_notified_at: datetime | None = None,
        now: datetime | None = None,
    ) -> Decision:
        """`streak_count` e a regra do D4: "X falhou 3x" e evento diferente de
        "X falhou 1x". Re_notify evita que um streak que continua-crescendo vire
        email a cada minuto."""
        now = now or datetime.now().astimezone()

        candidatos = self._by_type.get(event_type, [])
        if not candidatos:
            log.debug("sem_regra_para_evento", event_type=event_type)
            return Decision(
                notify=False, severity="info", template="generic",
                suppress_reason="sem_regra",
            )

        # maior min_streak que ainda for satisfeito, desempate por severidade
        aplicaveis = [r for r in candidatos if streak_count >= r.min_streak]
        if not aplicaveis:
            melhor = min(candidatos, key=lambda r: r.min_streak)
            return Decision(
                notify=False,
                severity=melhor.severity,
                template=melhor.template,
                suppress_reason="streak_abaixo_do_minimo",
            )
        regra = max(aplicaveis, key=lambda r: (r.min_streak, _RANK[r.severity]))

        if not regra.notify:
            return Decision(
                notify=False, severity=regra.severity, template=regra.template,
                suppress_reason="regra_marca_log_only",
            )

        # janela de re-notificacao. Por regra quando definida, global caso contrario.
        re_notificar = (
            regra.re_notify_after_minutes
            if regra.re_notify_after_minutes is not None
            else self.policy.aggregation.re_notify_after_minutes
        )
        if re_notificar > 0 and last_notified_at is not None and (
            now - last_notified_at < timedelta(minutes=re_notificar)
        ):
            return Decision(
                notify=False,
                severity=regra.severity,
                template=regra.template,
                suppress_reason=f"dentro_da_janela_de_renotificacao({re_notificar}min)",
            )

        # quiet hours -- critico sempre passa, o resto espera
        qh = self.policy.quiet_hours
        isencao = qh.enabled and qh.critical_breaks_quiet_hours and regra.severity == "critical"
        if qh.enabled and not isencao:
            local = self._em_fuso(now, qh.timezone)
            if dentro_de_quiet_hours(qh.start, qh.end, local):
                return Decision(
                    notify=False,
                    severity=regra.severity,
                    template=regra.template,
                    suppress_reason=f"quiet_hours({qh.start}-{qh.end})",
                )

        return Decision(
            notify=True,
            severity=regra.severity,
            template=regra.template,
            escalate_after_minutes=tuple(regra.escalate_after_minutes),
        )

    @staticmethod
    def _em_fuso(momento: datetime, tz: str) -> datetime:
        try:
            from zoneinfo import ZoneInfo

            return momento.astimezone(ZoneInfo(tz))
        except Exception:  # noqa: BLE001 - tz ausente no host nao pode derrubar o alerta
            return momento