"""Fila de disparo: um pendente por problema, janela de re-notificacao, ordem de saida."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from agent_nio_monitor.store.queue import (
    ATUALIZADO,
    ENFILEIRADO,
    SUPRIMIDO,
    InMemoryQueue,
    chave_de_dedup,
    payload_para_evento,
)
from agent_nio_monitor.store.repository import EventRecord

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def evento(tipo: str = "container_stopped", resource_id: str = "abc", **detail) -> EventRecord:
    return EventRecord(
        id=uuid4(), ts=T0, source="portainer", event_type=tipo, severity="critical",
        title=f"{tipo} {resource_id}", resource_id=resource_id, detail=detail,
    )


class TestDeduplicacao:
    def test_primeiro_aviso_entra_na_fila(self) -> None:
        q = InMemoryQueue()
        assert q.enqueue(evento(), ready_at=T0, renotify_minutes=720, now=T0) == ENFILEIRADO
        assert len(q.items) == 1

    def test_mesmo_problema_pendente_atualiza_em_vez_de_duplicar(self) -> None:
        q = InMemoryQueue()
        q.enqueue(evento("execution_failed", "wf", ocorrencias=1), ready_at=T0, renotify_minutes=720, now=T0)
        resultado = q.enqueue(evento("execution_failed", "wf", ocorrencias=3), ready_at=T0, renotify_minutes=720, now=T0)
        assert resultado == ATUALIZADO
        assert len(q.items) == 1
        assert q.items[0].payload["detail"]["ocorrencias"] == 3

    def test_problema_ja_enviado_dentro_da_janela_nao_volta(self) -> None:
        q = InMemoryQueue()
        q.enqueue(evento(), ready_at=T0, renotify_minutes=60, now=T0)
        q.mark_sent(q.items[0].id, T0)
        depois = T0 + timedelta(minutes=30)
        assert q.enqueue(evento(), ready_at=depois, renotify_minutes=60, now=depois) == SUPRIMIDO
        assert len(q.items) == 1

    def test_depois_da_janela_o_problema_pode_ser_avisado_de_novo(self) -> None:
        q = InMemoryQueue()
        q.enqueue(evento(), ready_at=T0, renotify_minutes=60, now=T0)
        q.mark_sent(q.items[0].id, T0)
        depois = T0 + timedelta(minutes=61)
        assert q.enqueue(evento(), ready_at=depois, renotify_minutes=60, now=depois) == ENFILEIRADO

    def test_fatos_nunca_se_fundem_cada_alteracao_e_um_aviso(self) -> None:
        q = InMemoryQueue()
        a, b = evento("workflow_changed", "wf"), evento("workflow_changed", "wf")
        assert chave_de_dedup(a) != chave_de_dedup(b)
        q.enqueue(a, ready_at=T0, renotify_minutes=0, now=T0)
        assert q.enqueue(b, ready_at=T0, renotify_minutes=0, now=T0) == ENFILEIRADO
        assert len(q.items) == 2

    def test_descartado_nao_bloqueia_um_novo_aviso(self) -> None:
        q = InMemoryQueue()
        q.enqueue(evento(), ready_at=T0, renotify_minutes=720, now=T0)
        q.mark_discarded(q.items[0].id, "resolvido", T0)
        assert q.enqueue(evento(), ready_at=T0, renotify_minutes=720, now=T0) == ENFILEIRADO


class TestSaida:
    def test_so_sai_o_que_ja_passou_do_prazo(self) -> None:
        q = InMemoryQueue()
        q.enqueue(evento(resource_id="cedo"), ready_at=T0, renotify_minutes=0, now=T0)
        q.enqueue(evento(resource_id="tarde"), ready_at=T0 + timedelta(seconds=40), renotify_minutes=0, now=T0)
        assert [i.resource_id for i in q.due(T0 + timedelta(seconds=10))] == ["cedo"]
        assert len(q.due(T0 + timedelta(seconds=41))) == 2

    def test_reagendado_so_volta_depois_da_espera(self) -> None:
        q = InMemoryQueue()
        q.enqueue(evento(), ready_at=T0, renotify_minutes=0, now=T0)
        q.mark_retry(q.items[0].id, "erro", T0 + timedelta(seconds=60))
        assert q.due(T0 + timedelta(seconds=30)) == []
        assert len(q.due(T0 + timedelta(seconds=61))) == 1
        assert q.items[0].attempts == 1

    def test_item_enviado_ou_descartado_nao_sai_de_novo(self) -> None:
        q = InMemoryQueue()
        q.enqueue(evento(resource_id="a"), ready_at=T0, renotify_minutes=0, now=T0)
        q.enqueue(evento(resource_id="b"), ready_at=T0, renotify_minutes=0, now=T0)
        q.mark_sent(q.items[0].id, T0)
        q.mark_discarded(q.items[1].id, "resolvido", T0)
        assert q.due(T0 + timedelta(minutes=5)) == []


class TestPayload:
    def test_evento_sobrevive_ida_e_volta_pelo_payload(self) -> None:
        original = evento("container_stopped", "abc", stack="comprove")
        original.tenant_id = "P1"
        q = InMemoryQueue()
        q.enqueue(original, ready_at=T0, renotify_minutes=0, now=T0)
        volta = payload_para_evento(q.items[0].payload)
        assert (volta.id, volta.event_type, volta.resource_id, volta.tenant_id) == (
            original.id, "container_stopped", "abc", "P1")
        assert volta.detail["stack"] == "comprove"
        assert volta.ts == original.ts

    def test_segredo_no_detalhe_nao_vai_para_o_payload(self) -> None:
        q = InMemoryQueue()
        q.enqueue(evento(password="hunter2"), ready_at=T0, renotify_minutes=0, now=T0)
        assert "hunter2" not in str(q.items[0].payload)


class TestManutencao:
    def test_stats_conta_pendentes_e_idade_do_mais_antigo(self) -> None:
        q = InMemoryQueue()
        q.enqueue(evento(resource_id="a"), ready_at=T0, renotify_minutes=0, now=T0)
        assert q.stats(T0 + timedelta(seconds=90)) == {"pending": 1, "oldest_pending_seconds": 90}

    def test_purge_remove_so_encerrados_antigos(self) -> None:
        q = InMemoryQueue()
        q.enqueue(evento(resource_id="velho"), ready_at=T0, renotify_minutes=0, now=T0)
        q.enqueue(evento(resource_id="pendente"), ready_at=T0, renotify_minutes=0, now=T0)
        q.mark_sent(q.items[0].id, T0)
        removidos = q.purge(T0 + timedelta(days=31))
        assert removidos == 1
        assert [i.resource_id for i in q.items] == ["pendente"]
