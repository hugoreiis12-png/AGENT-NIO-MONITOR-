"""Correlacao de fontes (ADR T4 do incidente MB de 28/10/2026): quando o log de
um container delata uma DEPENDENCIA do n8n, o agente consulta as execucoes dos
workflows declarados na ponte e anexa fatos deterministicos ao evento.

O caso que fechou a feature: o mcd-donalds pediu reset de senha, a ponte do
corretor respondeu viva, e 270 s depois o RESET_LINK_TIMEOUT matou o download.
O `MB Event Broker` estava verde em TODAS as execucoes da janela -- a quebra
estava no caminho IMAP->buffer, e nenhuma fonte isolada mostrava o nexo. So o
cruzamento deduz: "executou N vezes e devolveu lista vazia" e historia
diferente de "nao executou nenhuma vez".

Toda consulta e best-effort: uma fonte de erro aqui nunca derruba o alerta
original -- pior e perder a notificacao por causa de um enriquecimento.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import structlog

from .store.repository import EventRecord, new_event_id, now_utc

log = structlog.get_logger(__name__)

MAX_EXEC_POR_WORKFLOW = 25
ROTULO = {
    "nao_executou": "NAO executou na janela (provavel causa)",
    "com_erro": "executou COM ERROR",
    "respondeu_vazio": "executou verde e respondeu VAZIO (entrega nao chegou)",
    "entregou_dados": "executou e ENTREGOU dados",
    "falhou_apuracao": "consulta falhou (sem analise)",
}


class Correlacionador:
    """Consulta os workflows declarados nas pontes e escreve `detail.correlacao`.

    SHARED entre os dois consumidores: `Agent.check_container_logs` (enriquece o
    evento antes de decidir) e `InvestigationTools.correlacionar` (o Qwen cita
    o mesmo mapa quando investiga). Construido uma vez no `Agent`.
    """

    def __init__(self, n8n: Any, entradas: list[Any]) -> None:  # noqa: ANN401 - CorrelacaoConfig
        self.n8n = n8n
        self._tabelas: list[tuple[Any, list[re.Pattern[str]], list[str]]] = []
        for entrada in entradas or []:
            self._tabelas.append((
                entrada,
                [re.compile(p, re.IGNORECASE) for p in entrada.automacao],
                [m.casefold() for m in entrada.marcadores],
            ))

    @property
    def habilitado(self) -> bool:
        return bool(self._tabelas)

    def casar(self, nome: str, texto: str = "") -> Any | None:  # noqa: ANN401
        """Ponte que casa o nome (container/pipeline) OU um marcador no texto."""
        texto_f = (texto or "").casefold()
        for entrada, padroes, marcadores in self._tabelas:
            if any(p.search(nome or "") for p in padroes) or any(m in texto_f for m in marcadores):
                return entrada
        return None

    async def aplicar(self, eventos: list[EventRecord]) -> list[EventRecord]:
        """Enriquece `container_log_error` in-place; devolve eventos `dependency_silent` novos."""
        extras: list[EventRecord] = []
        for evento in eventos:
            if evento.event_type != "container_log_error":
                continue
            nome = str(evento.resource_id or evento.detail.get("recurso") or "")
            entrada = self.casar(nome, _texto_do_evento(evento))
            if entrada is None:
                continue
            fatos = await self.apurar(entrada)
            evento.detail["correlacao"] = fatos
            evento.detail["detalhe"] = (
                f"{evento.detail.get('detalhe') or ''}\n\n"
                f"Correlacoes (cruzamento de fontes):\n{_bloco(fatos)}"
            ).strip()
            if _dependencia_muda(fatos):
                culpa = next(f for f in fatos if f["leitura"] != "entregou_dados")
                extras.append(_evento_mudo(nome, entrada, culpa, fatos))
        return extras

    async def apurar(self, entrada: Any) -> list[dict[str, Any]]:  # noqa: ANN401
        """Fato por workflow mapeado, dentro da janela declarada. NUNCA levanta."""
        agora = now_utc()
        desde = agora - timedelta(minutes=entrada.janela_min)
        fatos: list[dict[str, Any]] = []
        for wf in entrada.workflows:
            fatos.append(await self._fato_do_workflow(wf, desde, agora, entrada.janela_min))
        return fatos

    async def _fato_do_workflow(self, wf: Any, desde: datetime, agora: datetime, janela_min: int) -> dict[str, Any]:  # noqa: ANN401
        try:
            execucoes = await self.n8n.recent_executions(wf.id, MAX_EXEC_POR_WORKFLOW)
            na_janela = [e for e in execucoes if _inicio(e) and _inicio(e) >= desde]
            com_erro = [e for e in na_janela if str(e.get("status")) == "error"]
            fato = {
                "workflow": wf.name, "workflow_id": wf.id,
                "execucoes": len(na_janela), "com_erro": len(com_erro),
                "janela_min": janela_min,
                "apurado_em": agora.isoformat(),
            }
            if na_janela:
                ultima = na_janela[0]
                fato["ultima"] = _inicio(ultima).isoformat()
                fato["status_ultima"] = str(ultima.get("status"))
            if not na_janela:
                fato["leitura"] = "nao_executou"
            elif com_erro:
                fato["leitura"] = "com_erro"
                fato["conteudo"] = await self._conteudo(na_janela[0]["id"])
            else:
                fato["conteudo"] = await self._conteudo(na_janela[0]["id"])
                fato["leitura"] = "respondeu_vazio" if _vazio(fato["conteudo"]) else "entregou_dados"
            return fato
        except Exception as exc:  # noqa: BLE001 - apuracao nunca derruba o alerta
            log.warning("correlacao_falhou_no_workflow", workflow=wf.name, erro=f"{type(exc).__name__}: {exc}")
            return {"workflow": wf.name, "workflow_id": wf.id, "leitura": "falhou_apuracao",
                    "erro": f"{type(exc).__name__}: {str(exc)[:200]}", "janela_min": janela_min}

    async def _conteudo(self, execucao_id: str) -> str | None:
        """O que a ultima execucao da janela DEVOLVEU (resumo por texto, redigido)."""
        detalhe = await self.n8n.get_execution(execucao_id, include_data=True)
        runs = (((detalhe or {}).get("data") or {}).get("resultData") or {}).get("runData") or {}
        partes: list[str] = []
        for no, execucoes in runs.items():
            itens = 0
            notas: list[str] = []
            for execucao in execucoes or ():
                for saida in (execucao.get("data") or {}).get("main") or []:
                    for item in saida or []:
                        itens += 1
                        json_out = item.get("json") if isinstance(item, dict) else None
                        if isinstance(json_out, dict) and isinstance(json_out.get("eventos"), list):
                            tipos = {}
                            for ev in json_out["eventos"]:
                                tipo = str((ev or {}).get("type") or "?")
                                tipos[tipo] = tipos.get(tipo, 0) + 1
                            notas.append("eventos: " + ", ".join(f"{t} x{n}" for t, n in tipos.items()))
            partes.append(f"{no}: {itens} item(ns)" + (f" ({notas[0]})" if notas else ""))
        if not partes:
            return None
        return "; ".join(partes)


def _vazio(conteudo: str | None) -> bool:
    """Tudo zero item = resposta sem entrega. Contem 'eventos' com x0 = idem."""
    if not conteudo:
        return True
    return all(part.endswith("0 item(ns)") for part in conteudo.split("; "))


def _inicio(execucao: dict[str, Any]) -> datetime | None:
    bruto = execucao.get("startedAt")
    if not bruto:
        return None
    try:
        return datetime.fromisoformat(str(bruto).replace("Z", "+00:00"))
    except ValueError:
        return None


def _dependencia_muda(fatos: list[dict[str, Any]]) -> bool:
    """Muda = nao entregou o que a automacao esperou: nao rodou, errou ou respondeu vazio."""
    return any(f["leitura"] in ("nao_executou", "com_erro", "respondeu_vazio") for f in fatos)


def _texto_do_evento(evento: EventRecord) -> str:
    partes = [str(evento.title), str(evento.detail.get("erro") or ""), str(evento.detail.get("logs") or "")]
    return " ".join(partes)


def _bloco(fatos: list[dict[str, Any]]) -> str:
    """Texto deterministico que entra no e-mail e no prompt do LLM (detail.correlacao_txt)."""
    linhas = []
    for f in fatos:
        linha = f"{f['workflow']} ({f['workflow_id']}): {ROTULO.get(f['leitura'], f['leitura'])}"
        if f["leitura"] == "nao_executou":
            linha += f" -- 0 execucoes na janela de {f['janela_min']} min"
        elif "execucoes" in f:
            linha += f" -- {f['execucoes']} execucao(oes) na janela de {f['janela_min']} min"
        if f.get("conteudo"):
            linha += f" -- saida: {str(f['conteudo'])[:300]}"
        linhas.append(linha)
    return "\n".join(linhas)


def _evento_mudo(nome: str, entrada: Any, culpa: dict[str, Any], fatos: list[dict[str, Any]]) -> EventRecord:  # noqa: ANN401
    """`dependency_silent`: a automacao pagou a espera e a dependencia nao entregou.

    resource_id = o container/automacao: a dedup por chave (event_type, resource_id)
    da fila garante UM pendente por problema, e `re_notify_after_minutes` da regra
    governa a repeticao.
    """
    correu = culpa["leitura"]
    detalhe = (
        f"A automacao '{nome}' esperou a entrega do n8n e nao recebeu: "
        f"o workflow '{culpa['workflow']}' {ROTULO[correu]}."
    )
    return EventRecord(
        id=new_event_id(), ts=now_utc(), source="n8n", event_type="dependency_silent", severity="high",
        title=f"Dependencia muda: {nome} sem entrega de {culpa['workflow']}", resource_id=nome,
        detail={
            "recurso": nome, "workflow_nome": nome, "automacao": nome,
            "workflows": [wf.name for wf in entrada.workflows],
            "correlacao": fatos,
            "detalhe": (
                f"{detalhe}\n\n"
                f"Correlacoes (cruzamento de fontes):\n{_bloco(fatos)}"
            ),
        },
    )
