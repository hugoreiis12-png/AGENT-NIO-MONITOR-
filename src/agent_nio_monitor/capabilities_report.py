"""Saidas do `CapabilityReport`: a tabela humana do `doctor` e as linhas para o banco."""

from __future__ import annotations

from typing import Any

from .capabilities import CapabilityReport


def render_table(report: CapabilityReport) -> str:
    """Saida humana do boot -- e a evidencia que responde 'o que a instancia expoe?'.

    Sobre a versao: eu afirmei antes que `GET /healthz` devolvia
    `{"status":"ok","version":"1.x.y"}`. NAO E VERDADE nesta instalacao --
    verificado, devolve so `{"status":"ok"}`, e a UI nao tem versao no HTML.

    Por isso a versao e um campo OPCIONAL. O que decide o comportamento do agente
    e a deteccao de CAPACIDADES (quais endpoints existem), que e medida de
    verdade. Ausencia de versao e registrada, nunca estimada.
    """
    linhas = [
        "fonte      feature       rota                                        disp  detalhe",
        "-" * 96,
    ]
    for src, feat, path, ok, detail in report.as_rows():
        linhas.append(f"{src:<11} {feat:<13} {path:<43} {'SIM' if ok else 'nao':<5} {detail}")
    linhas.append("-" * 96)
    if report.source != "n8n":
        # A nota de versao e sobre o n8n. Repetir no Portainer (que nem tem
        # /healthz na sonda) produz texto que nao se aplica.
        return "\n".join(linhas)
    if report.n8n_version:
        linhas.append(f"VERSAO REPORTADA PELO N8N: {report.n8n_version}")
    else:
        linhas.append(
            "VERSAO: nao exposta por esta instancia (/healthz devolve so status).\n"
            "        Decisao por CAPACIDADES acima, que e medida -- e nao estimada."
        )
    return "\n".join(linhas)

def capability_rows(report: CapabilityReport) -> list[dict[str, Any]]:
    return [
        {
            "source": c.source,
            "feature": c.feature,
            "path": c.path,
            "available": c.available,
            "status_code": c.status_code,
            "detail": c.detail,
        }
        for c in report.items
    ]
