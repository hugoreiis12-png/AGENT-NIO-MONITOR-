"""Ferramentas SO DE LEITURA que o Qwen chama para apurar a causa de um alerta (ADR D42).

Cada ferramenta devolve texto redigido e curto, nunca levanta (erro vira texto que o modelo le)
e nunca expoe `Env` de container nem dado de item do n8n. O conjunto oferecido depende da fonte
do evento: o modelo de um alerta de RPA nao ve ferramenta de Docker.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from .redaction import redact_text
from .sources.n8n.client import N8NClient
from .sources.n8n.collector import extrair_erro
from .sources.n8n.node_path import erros_escondidos, resumo_do_caminho, run_data
from .sources.portainer.client import PortainerClient
from .sources.powerautomate.client import DataverseClient
from .sources.powerautomate.session_detail import analisar_acoes, fase_com_falha

MAX_SAIDA = 6000
MAX_LINHAS_DE_LOG = 300


def _ferramenta(nome: str, descricao: str, propriedades: dict[str, Any], obrigatorias: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": nome, "description": descricao,
        "parameters": {"type": "object", "properties": propriedades, "required": obrigatorias},
    }}


_TEXTO, _INTEIRO = {"type": "string"}, {"type": "integer"}
ESQUEMAS = {
    "execucao_n8n": _ferramenta(
        "execucao_n8n", "Detalhe de uma execucao do n8n: status, erro, ultimo no e caminho no a no.",
        {"execucao_id": _TEXTO}, ["execucao_id"]),
    "ultimas_execucoes_n8n": _ferramenta(
        "ultimas_execucoes_n8n", "Ultimas execucoes de um workflow (status, inicio, modo): mostra padrao e recorrencia.",
        {"workflow_id": _TEXTO, "quantidade": _INTEIRO}, ["workflow_id"]),
    "log_do_container": _ferramenta(
        "log_do_container", "Ultimas linhas do log de um container (opcional: so linhas que contem `filtro`).",
        {"container": _TEXTO, "linhas": _INTEIRO, "filtro": _TEXTO}, ["container"]),
    "inspecionar_container": _ferramenta(
        "inspecionar_container", "Estado do container: status, exit code, OOMKilled, healthcheck, reinicios, imagem.",
        {"container": _TEXTO}, ["container"]),
    "sessoes_rpa": _ferramenta(
        "sessoes_rpa", "Ultimas sessoes de um fluxo de RPA: status, codigo de erro, maquina e fase.",
        {"fluxo_id": _TEXTO, "quantidade": _INTEIRO}, ["fluxo_id"]),
    "acoes_da_sessao_rpa": _ferramenta(
        "acoes_da_sessao_rpa", "Acoes de uma sessao de RPA: quais falharam, em que subfluxo e com que erro.",
        {"sessao_id": _TEXTO}, ["sessao_id"]),
}
POR_FONTE = {
    "n8n": ("execucao_n8n", "ultimas_execucoes_n8n", "log_do_container"),
    "portainer": ("log_do_container", "inspecionar_container"),
    "powerautomate": ("sessoes_rpa", "acoes_da_sessao_rpa"),
}
# Ferramenta -> cliente que ela exige (fonte desligada = ferramenta fora da lista).
REQUER = {
    "log_do_container": "portainer", "inspecionar_container": "portainer",
    "sessoes_rpa": "powerautomate", "acoes_da_sessao_rpa": "powerautomate",
}


def _saida(dados: Any) -> str:
    texto = dados if isinstance(dados, str) else json.dumps(dados, ensure_ascii=False, default=str)
    return redact_text(texto)[:MAX_SAIDA]


class InvestigationTools:
    def __init__(
        self, *, n8n: N8NClient, portainer: PortainerClient | None, dataverse: DataverseClient | None,
    ) -> None:
        self.n8n, self.portainer, self.dataverse = n8n, portainer, dataverse

    def esquemas(self, fonte: str) -> list[dict]:
        clientes = {"portainer": self.portainer, "powerautomate": self.dataverse}
        return [
            ESQUEMAS[nome] for nome in POR_FONTE.get(fonte, ())
            if nome not in REQUER or clientes[REQUER[nome]] is not None
        ]

    async def executar(self, nome: str, argumentos: str | dict) -> str:
        funcao: Callable[..., Awaitable[Any]] | None = getattr(self, f"_{nome}", None) if nome in ESQUEMAS else None
        if funcao is None:
            return f"erro: ferramenta desconhecida {nome}"
        try:
            args = json.loads(argumentos) if isinstance(argumentos, str) else dict(argumentos or {})
            return _saida(await funcao(**args))
        except Exception as exc:  # noqa: BLE001 - erro de ferramenta vira texto para o modelo, nunca derruba o envio
            return f"erro: {type(exc).__name__}: {redact_text(str(exc))[:300]}"

    async def _execucao_n8n(self, execucao_id: str) -> dict:
        detalhe = await self.n8n.get_execution(execucao_id, include_data=True)
        erro, no = extrair_erro(detalhe)
        runs = run_data(detalhe)
        return {
            "status": detalhe.get("status"), "modo": detalhe.get("mode"), "inicio": detalhe.get("startedAt"),
            "fim": detalhe.get("stoppedAt"), "erro": erro, "ultimo_no": no,
            "erros_de_no": erros_escondidos(detalhe.get("workflowData") or {}, runs),
            "caminho": resumo_do_caminho(runs),
        }

    async def _ultimas_execucoes_n8n(self, workflow_id: str, quantidade: int = 10) -> list[dict]:
        execucoes = await self.n8n.recent_executions(workflow_id, max(1, min(int(quantidade), 25)))
        return [{k: e.get(k) for k in ("id", "status", "startedAt", "stoppedAt", "mode")} for e in execucoes]

    async def _achar_container(self, nome: str) -> tuple[int, dict]:
        for endpoint in await self.portainer.list_endpoints():
            for row in await self.portainer.list_containers(int(endpoint["Id"])):
                if nome in [n.lstrip("/") for n in row.get("Names") or []]:
                    return int(endpoint["Id"]), row
        raise LookupError(f"container {nome} nao encontrado")

    async def _log_do_container(self, container: str, linhas: int = 150, filtro: str | None = None) -> str:
        endpoint_id, row = await self._achar_container(container)
        texto = await self.portainer.container_logs(
            endpoint_id, str(row["Id"]), tail=max(10, min(int(linhas), MAX_LINHAS_DE_LOG)), timestamps=True,
        )
        selecionadas = [linha for linha in texto.splitlines() if not filtro or filtro.lower() in linha.lower()]
        return "\n".join(selecionadas)[-MAX_SAIDA:] or "(nenhuma linha)"

    async def _inspecionar_container(self, container: str) -> dict:
        endpoint_id, row = await self._achar_container(container)
        detalhe = await self.portainer.inspect_container(endpoint_id, str(row["Id"]))
        estado = detalhe.get("State") or {}
        saude = estado.get("Health") or {}
        return {
            "status": estado.get("Status"), "exit_code": estado.get("ExitCode"), "oom_killed": estado.get("OOMKilled"),
            "erro": estado.get("Error"), "iniciado": estado.get("StartedAt"), "terminado": estado.get("FinishedAt"),
            "health": saude.get("Status"), "health_ultimos": [h.get("Output") for h in (saude.get("Log") or [])[-3:]],
            "reinicios": detalhe.get("RestartCount"), "imagem": (detalhe.get("Config") or {}).get("Image"),
            "limite_memoria": (detalhe.get("HostConfig") or {}).get("Memory"),
        }

    async def _sessoes_rpa(self, fluxo_id: str, quantidade: int = 5) -> list[dict]:
        dados = await self.dataverse.get("flowsessions", {
            "$select": "flowsessionid,statuscode,errorcode,errormessage,completedon,rundetails",
            "$filter": f"_regardingobjectid_value eq {fluxo_id.removeprefix('pa:')}",
            "$orderby": "completedon desc", "$top": str(max(1, min(int(quantidade), 15))),
        })
        return [{
            "sessao_id": s.get("flowsessionid"), "status": s.get("statuscode"), "codigo": s.get("errorcode"),
            "mensagem": s.get("errormessage"), "fim": s.get("completedon"), "fase": fase_com_falha(s.get("rundetails")),
        } for s in dados.get("value", [])]

    async def _acoes_da_sessao_rpa(self, sessao_id: str) -> dict:
        conteudo = await self.dataverse.arquivo(f"flowsessions({sessao_id})", "additionalcontext")
        if conteudo is None:
            return {"log": "indisponivel (sessao nao executou ou log ainda nao subiu)"}
        analise = analisar_acoes(conteudo)
        return {
            "total_de_acoes": analise.total, "falha": analise.falha.descricao() if analise.falha else None,
            "falhas_tratadas": [a.descricao() for a in analise.tratadas],
        }
