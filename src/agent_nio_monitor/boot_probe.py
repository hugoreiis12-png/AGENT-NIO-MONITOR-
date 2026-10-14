"""Sonda de capacidade no boot (ADR D2): sonda, grava, adapta."""

from __future__ import annotations

import structlog

from .capabilities import detect, fontes_do_config
from .capabilities_report import capability_rows
from .config.schema import AppConfig
from .store.repository import Repository

log = structlog.get_logger(__name__)


async def sondar_no_boot(cfg: AppConfig, repo: Repository) -> dict[str, int]:
    """D2 em acao no boot: sonda, grava, adapta.

    Antes isto so rodava em `doctor`, por mao, e o resultado ia para
    `log.debug`. `save_capabilities` nunca era chamado em producao -- a
    tabela `capabilities` ficava vazia enquanto o ADR D2 dizia que o
    boot sondava. A docstring de `capabilities.py` ja prometia "a versao
    real aparece no boot", e nao aparecia.

    NUNCA levanta. Uma sonda que falha tem que deixar o agente comecar a
    coletar e dizer que nao sondou; o contrario e trocar um alerta perdido
    por um agente inteiro parado.
    """
    fontes = fontes_do_config(cfg)
    if not fontes:
        log.warning("capability_sem_credencial", efeito="nenhuma fonte sondada")
        return {"fontes": 0, "capacidades": 0}
    try:
        reports = await detect(fontes)
    except Exception as exc:  # noqa: BLE001 - sondar nunca pode derrubar o boot
        log.warning("sonda_de_capacidade_falhou", erro=f"{type(exc).__name__}: {exc}")
        return {"fontes": len(fontes), "capacidades": 0}

    linhas = [linha for rep in reports.values() for linha in capability_rows(rep)]
    if linhas:
        repo.save_capabilities(linhas)
    ausentes = [c.path for rep in reports.values() for c in rep.missing_core()]
    log.info(
        "capabilities_detectadas_no_boot",
        fontes=list(reports),
        capacidades=len(linhas),
        ausentes_de_core=ausentes,
    )
    return {"fontes": len(reports), "capacidades": len(linhas)}
