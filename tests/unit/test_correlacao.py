"""Correlacao de fontes: o cross-check que separa 'n8n verde' de 'entrega ok'.

Fixture real: incidente 28/10/2026 07:00 -- mcd-donalds pediu reset de senha, a
ponte respondeu viva, 270 s de espera, RESET_LINK_TIMEOUT. O `MB Event Broker`
executou verde NA JANELA (respondeu lista vazia) e o `MB Email IMAP` nao
depositou nada: 'a ponte respondeu' nunca foi 'a ponte entregou'. Sao esses
fatos que o modulo precisa deduzir deterministico, sem LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_nio_monitor.config.schema import (
    CorrelacaoConfig,
    CorrelacaoWorkflow,
    NotificationRule,
    PolicyConfig,
)
from agent_nio_monitor.correlacao import Correlacionador
from agent_nio_monitor.rules.engine import RuleEngine
from agent_nio_monitor.store.repository import EventRecord, now_utc

ELTON_FIX = "RESET_LINK_TIMEOUT Link de redefinicao nao chegou em 270s — Confira as execuções do fluxo de trabalho 'MB Email IMAP' no n8n."


def _entrada(**kw) -> CorrelacaoConfig:
    base = dict(
        automacao=["mcd.?donalds"],
        marcadores=["RESET_LINK_TIMEOUT", "execuções do fluxo de trabalho"],
        workflows=[
            CorrelacaoWorkflow(name="MB Email IMAP", id="D5TsMY8mOJj6gJ1L"),
            CorrelacaoWorkflow(name="MB Event Broker", id="STfrlbTnTeuBDntf"),
        ],
        janela_min=15,
    )
    base.update(kw)
    return CorrelacaoConfig(**base)


def _eventos_do_n8n(*execucoes: dict, horas: float = 0.0) -> list[dict]:
    """Execuacoes relativas a 'agora'. `horas` negativas = atras."""
    return [
        {
            "id": str(e.get("id", i)),
            "status": e.get("status", "success"),
            "startedAt": (now_utc() - timedelta(hours=horas)).isoformat(),
        }
        for i, e in enumerate(execucoes or [{}])
    ]


class N8NFalso:
    def __init__(self, por_workflow: dict[str, list[dict]], conteudos: dict[str, dict] | None = None) -> None:
        self._por = por_workflow
        self._conteudos = conteudos or {}

    async def recent_executions(self, workflow_id: str, limite: int) -> list[dict]:
        return self._por.get(workflow_id, [])

    async def get_execution(self, execucao_id: str, include_data: bool = True) -> dict:
        return self._conteudos.get(execucao_id, {})


def _run_data(ler_eventos: list[dict] | None) -> dict:
    """Formato da API: runData['Ler'] com json{eventos} (o no do Broker)."""
    saida = [] if ler_eventos is None else [{"json": {"eventos": ler_eventos}}]
    return {"data": {"resultData": {"runData": {"Ler": [{"data": {"main": [saida]}}]}}}}


def _evento_log(texto: str, recurso: str = "mcd-donalds") -> EventRecord:
    return EventRecord(
        id=uuid4(), ts=now_utc(), source="portainer", event_type="container_log_error",
        severity="medium", title=f"Erro no log de {recurso}", resource_id=recurso,
        detail={"recurso": recurso, "erro": texto, "detalhe": "linha original"},
    )


SEGUIDO = {"status": "success", "id": "89159"}
ESCONDERIJO = {"reset_link": [{"type": "reset_link"}, {"type": "reset_link"}]}

# ------------------------------------------------------------------- casar


class TestCasar:
    async def test_casa_pelo_nome_do_container(self) -> None:
        c = Correlacionador(N8NFalso({}), [_entrada()])
        assert c.casar("mcd-donalds") is not None
        assert c.casar("MCD_Donalds-worker") is not None

    async def test_casa_pelo_marcador_no_texto_quando_o_nome_nao_diz_nada(self) -> None:
        c = Correlacionador(N8NFalso({}), [_entrada()])
        assert c.casar("outro-container", ELTON_FIX) is not None

    async def test_nao_casa_fora_do_mapa(self) -> None:
        c = Correlacionador(N8NFalso({}), [_entrada()])
        assert c.casar("postgres_exporter", "tempo de query alto") is None

# ------------------------------------------------------------------- apurar


class TestApurar:
    async def test_workflow_que_nao_executou_na_janela_e_o_culpado(self) -> None:
        n8n = N8NFalso({
            "D5TsMY8mOJj6gJ1L": [],                                # IMAP mudo
            "STfrlbTnTeuBDntf": _eventos_do_n8n(SEGUIDO, horas=0),
        })
        c = Correlacionador(n8n, [_entrada()])
        fatos = await c.apurar(_entrada())
        leituras = {f["workflow"]: f["leitura"] for f in fatos}
        assert leituras["MB Email IMAP"] == "nao_executou"
        assert leituras["MB Event Broker"] == "respondeu_vazio"

    async def test_entrega_real_dados_nao_e_mudanca(self) -> None:
        n8n = N8NFalso(
            {"STfrlbTnTeuBDntf": _eventos_do_n8n(SEGUIDO, horas=0)},
            {"89159": _run_data(ESCONDERIJO["reset_link"])},
        )
        config = _entrada(
            workflows=[CorrelacaoWorkflow(name="MB Event Broker", id="STfrlbTnTeuBDntf")],
        )
        c = Correlacionador(n8n, [config])
        fatos = await c.apurar(config)
        assert fatos[0]["leitura"] == "entregou_dados"
        assert "reset_link" in (fatos[0].get("conteudo") or "")

    async def test_execucao_fora_da_janela_conta_como_nao_executou(self) -> None:
        # Execucao ha 3h: startedAt fora da janela de 15 min => nao conta.
        antiga = {"id": "1", "status": "success", "startedAt": (now_utc() - timedelta(hours=3)).isoformat()}
        n8n = N8NFalso({"STfrlbTnTeuBDntf": [antiga]}, {})
        c = Correlacionador(n8n, [_entrada()])
        fatos = await c.apurar(_entrada())
        assert fatos[0]["leitura"] == "nao_executou"

    async def test_execucao_com_erro_na_janela_vira_fato(self) -> None:
        erro = {"id": "2", "status": "error", "startedAt": now_utc().isoformat()}
        n8n = N8NFalso({"D5TsMY8mOJj6gJ1L": _eventos_do_n8n(erro, horas=0)}, {})
        c = Correlacionador(n8n, [_entrada()])
        fatos = await c.apurar(_entrada())
        assert fatos[0]["leitura"] == "com_erro"
        assert fatos[0]["com_erro"] == 1

    async def test_n8n_erro_nunca_derruba_o(self) -> None:
        class N8NDoente:
            async def recent_executions(self, workflow_id: str, limite: int) -> list[dict]:
                raise RuntimeError("timeout")

        c = Correlacionador(N8NDoente(), [_entrada()])
        fatos = await c.apurar(_entrada())
        assert all(f["leitura"] == "falhou_apuracao" for f in fatos)
        assert all("runtime" in f.get("erro", "").lower() or "RuntimeError" in f.get("erro", "") for f in fatos)

# ------------------------------------------------------------------- aplicar


class TestAplicar:
    async def test_evento_de_log_ganha_correlacao_e_detalhe(self) -> None:
        n8n = N8NFalso({
            "D5TsMY8mOJj6gJ1L": [],
            "STfrlbTnTeuBDntf": _eventos_do_n8n(SEGUIDO, horas=0),
        })
        c = Correlacionador(n8n, [_entrada()])
        evento = _evento_log(ELTON_FIX)
        await c.aplicar([evento])
        assert "correlacao" in evento.detail
        assert "Correlacoes (cruzamento de fontes):" in evento.detail["detalhe"]
        assert "MB Email IMAP" in evento.detail["detalhe"]

    async def test_dependencia_muda_emite_dependency_silent(self) -> None:
        n8n = N8NFalso({
            "D5TsMY8mOJj6gJ1L": [],
            "STfrlbTnTeuBDntf": _eventos_do_n8n(SEGUIDO, horas=0),
        })
        c = Correlacionador(n8n, [_entrada()])
        extras = await c.aplicar([_evento_log(ELTON_FIX)])
        assert len(extras) == 1
        silencioso = extras[0]
        assert silencioso.event_type == "dependency_silent"
        assert silencioso.severity == "high"
        assert silencioso.resource_id == "mcd-donalds"
        assert "MB Email IMAP" in silencioso.title

    async def test_entrega_ok_nao_emite_dependency_silent(self) -> None:
        n8n = N8NFalso(
            {"STfrlbTnTeuBDntf": _eventos_do_n8n(SEGUIDO, horas=0)},
            {"89159": _run_data(ESCONDERIJO["reset_link"])},
        )
        config = _entrada(
            workflows=[CorrelacaoWorkflow(name="MB Event Broker", id="STfrlbTnTeuBDntf")],
        )
        c = Correlacionador(n8n, [config])
        extras = await c.aplicar([_evento_log(ELTON_FIX)])
        assert extras == []

    async def test_evento_de_outro_tipo_nao_e_tocado(self) -> None:
        evento = EventRecord(id=uuid4(), ts=now_utc(), source="n8n", event_type="execution_failed",
                             severity="high", title="wf", resource_id="wf1", detail={})
        n8n = N8NFalso({}, {})
        c = Correlacionador(n8n, [_entrada()])
        assert await c.aplicar([evento]) == []
        assert "correlacao" not in evento.detail

# ------------------------------------------------------------------- config


class TestConfig:
    def test_regex_invalido_rejeita_no_boot(self) -> None:
        with pytest.raises(ValidationError):
            _entrada(automacao=["mcd[donalds"])

    def test_chave_desconhecida_e_recusada(self) -> None:
        with pytest.raises(ValidationError):
            _entrada(cadencia=30)

    def test_sem_correlacoes_a_policy_e_valida(self) -> None:
        policy = PolicyConfig(rules=[NotificationRule(name="x", event_types=["execution_failed"])])
        assert policy.correlacoes == []

# ------------------------------------------------------------------- regra


class TestRegra:
    def test_dependency_silent_notifica_alto_com_template_proprio(self) -> None:
        regra = NotificationRule(name="dependencia_muda", event_types=["dependency_silent"],
                                 severity="high", template="dependency_silent")
        engine = RuleEngine(PolicyConfig(rules=[regra]))
        decisao = engine.evaluate(event_type="dependency_silent")
        assert (decisao.notify, decisao.severity, decisao.template) == (True, "high", "dependency_silent")

# ------------------------------------------------------------------- template


class TestTemplate:
    async def test_assunto_e_corpo_do_dependency_silent(self) -> None:
        from agent_nio_monitor.notify.templates import render

        evento = EventRecord(
            id=uuid4(), ts=datetime.now(tz=UTC), source="n8n", event_type="dependency_silent",
            severity="high", title="Dependencia muda: mcd-donalds sem entrega de MB Email IMAP",
            resource_id="mcd-donalds",
            detail={"detalhe": "A automacao 'mcd-donalds' esperou a entrega do n8n e nao recebeu: "
                               "o workflow 'MB Email IMAP' NAO executou na janela.",
                    "correlacao": [{"workflow": "MB Email IMAP", "workflow_id": "D5TsMY8mOJj6gJ1L",
                                    "leitura": "nao_executou", "execucoes": 0, "janela_min": 15}]},
        )
        assunto, corpo = render(
            "dependency_silent",
            event=evento,
            contexto={"detalhe": evento.detail["detalhe"], "fonte": "n8n"},
        )
        assert assunto.startswith("★ [ALTO] Dependencia muda: mcd-donalds")  # ★ = marca do n8n
        assert "NAO executou" in corpo  # detalhe no corpo
        assert "reativar" in corpo  # acao_padrao referencia reativar
