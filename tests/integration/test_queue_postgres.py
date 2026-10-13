"""PostgresQueue contra Postgres REAL (so DATABASE_DSN_TEST; pula sem ele).

Mesmos contratos do InMemoryQueue -- mais o que so o banco garante: o indice unico
parcial impede dois pendentes da mesma chave, e a fila sobrevive a reinicio.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from agent_nio_monitor.store.queue import (
    ATUALIZADO,
    ENFILEIRADO,
    SUPRIMIDO,
    PostgresQueue,
    payload_para_evento,
)
from agent_nio_monitor.store.repository import EventRecord

from .dsn_teste import dsn_de_teste

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

DSN = dsn_de_teste()
T0 = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
pytestmark = pytest.mark.integration


@pytest.fixture
def fila():  # noqa: ANN201
    if not DSN:
        pytest.skip("DATABASE_DSN_TEST ausente ou inseguro (precisa ser base *_test e diferente do agente)")
    from agent_nio_monitor.store.db import Database

    db = Database(DSN, "hermes", 1, 4)
    db.open()
    db.migrate()
    with db.connection() as conn:
        conn.execute("TRUNCATE hermes.notification_queue")
        conn.commit()
    yield PostgresQueue(db)
    db.close()


def _ev(tipo: str = "container_stopped", recurso: str = "abc", **detail) -> EventRecord:
    return EventRecord(id=uuid4(), ts=T0, source="portainer", event_type=tipo, severity="critical",
                       title=f"{tipo}", resource_id=recurso, detail=detail)


class TestContratos:
    def test_enfileira_atualiza_e_suprime(self, fila) -> None:  # noqa: ANN001
        assert fila.enqueue(_ev(ocorrencias=1), ready_at=T0, renotify_minutes=60, now=T0) == ENFILEIRADO
        assert fila.enqueue(_ev(ocorrencias=3), ready_at=T0, renotify_minutes=60, now=T0) == ATUALIZADO
        item = fila.due(T0)[0]
        assert payload_para_evento(item.payload).detail["ocorrencias"] == 3
        fila.mark_sent(item.id, T0)
        assert fila.enqueue(_ev(), ready_at=T0, renotify_minutes=60, now=T0 + timedelta(minutes=5)) == SUPRIMIDO
        assert fila.enqueue(_ev(), ready_at=T0, renotify_minutes=60, now=T0 + timedelta(minutes=61)) == ENFILEIRADO

    def test_so_sai_depois_do_prazo_e_respeita_o_reagendamento(self, fila) -> None:  # noqa: ANN001
        fila.enqueue(_ev(), ready_at=T0 + timedelta(seconds=35), renotify_minutes=0, now=T0)
        assert fila.due(T0 + timedelta(seconds=10)) == []
        item = fila.due(T0 + timedelta(seconds=36))[0]
        fila.mark_retry(item.id, "erro", T0 + timedelta(seconds=100))
        assert fila.due(T0 + timedelta(seconds=60)) == []
        retomado = fila.due(T0 + timedelta(seconds=101))[0]
        assert retomado.attempts == 1

    def test_descartado_e_falho_nao_voltam(self, fila) -> None:  # noqa: ANN001
        fila.enqueue(_ev(recurso="a"), ready_at=T0, renotify_minutes=0, now=T0)
        fila.enqueue(_ev(recurso="b"), ready_at=T0, renotify_minutes=0, now=T0)
        a, b = fila.due(T0)
        fila.mark_discarded(a.id, "container voltou a rodar", T0)
        fila.mark_failed(b.id, "sem destinatario")
        assert fila.due(T0 + timedelta(minutes=5)) == []

    def test_fatos_nao_se_fundem(self, fila) -> None:  # noqa: ANN001
        fila.enqueue(_ev("workflow_changed", "wf"), ready_at=T0, renotify_minutes=0, now=T0)
        assert fila.enqueue(_ev("workflow_changed", "wf"), ready_at=T0, renotify_minutes=0, now=T0) == ENFILEIRADO
        assert len(fila.due(T0)) == 2

    def test_stats_e_purge(self, fila) -> None:  # noqa: ANN001
        fila.enqueue(_ev(recurso="a"), ready_at=T0, renotify_minutes=0, now=T0)
        fila.enqueue(_ev(recurso="b"), ready_at=T0, renotify_minutes=0, now=T0)
        assert fila.stats(T0 + timedelta(seconds=45)) == {"pending": 2, "oldest_pending_seconds": 45}
        fila.mark_sent(fila.due(T0)[0].id, T0)
        assert fila.purge(T0 + timedelta(days=31)) == 1
        assert fila.stats(T0)["pending"] == 1

    def test_segredo_no_detalhe_nao_chega_ao_banco(self, fila) -> None:  # noqa: ANN001
        fila.enqueue(_ev(password="hunter2"), ready_at=T0, renotify_minutes=0, now=T0)
        assert "hunter2" not in str(fila.due(T0)[0].payload)


class TestSobreviveAReinicio:
    def test_outra_instancia_enxerga_o_que_ficou_pendente(self, fila) -> None:  # noqa: ANN001
        fila.enqueue(_ev(recurso="worker"), ready_at=T0, renotify_minutes=0, now=T0)
        reiniciada = PostgresQueue(fila.db)
        assert [i.resource_id for i in reiniciada.due(T0)] == ["worker"]
