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
    LEASE_SEGUNDOS,
    SUPRIMIDO,
    payload_para_evento,
)
from agent_nio_monitor.store.postgres_queue import PostgresQueue
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


class TestReservaDoDue:
    """`due()` reivindica a linha, nao so a le.

    Antes era `SELECT *`: nenhum estado mudava, entao uma segunda leitura --
    de outro despachante, ou de um `flush` sobreposto -- devolvia a MESMA linha
    e mandava o mesmo e-mail duas vezes. Seguro so porque havia um unico laco
    serializado em `main._agenda`.
    """

    def test_segunda_leitura_nao_devolve_o_que_foi_reservado(self, fila) -> None:  # noqa: ANN001
        fila.enqueue(_ev(recurso="worker"), ready_at=T0, renotify_minutes=0, now=T0)

        assert len(fila.due(T0)) == 1
        assert fila.due(T0) == [], "o item foi reservado e nao pode sair de novo"

    def test_reserva_expira_e_o_item_volta(self, fila) -> None:  # noqa: ANN001
        """Se o processo morre no meio do envio, o item volta sozinho. E o que
        impede um crash de transformar um alerta pendente em alerta perdido."""
        fila.enqueue(_ev(recurso="worker"), ready_at=T0, renotify_minutes=0, now=T0)
        fila.due(T0)

        assert fila.due(T0 + timedelta(seconds=LEASE_SEGUNDOS - 1)) == []
        assert len(fila.due(T0 + timedelta(seconds=LEASE_SEGUNDOS + 1))) == 1

    def test_resolver_nao_traz_de_volta_mesmo_expirada_a_reserva(self, fila) -> None:  # noqa: ANN001
        """`due()` reserva TUDO que devolve -- o limite e 50, nao 1.

        Um item marcado como enviado nao volta nem depois de a reserva expirar,
        porque o status (nao a reserva) e o que define elegibilidade. Sem este
        teste, `mark_sent` poderia ter parado de limpar a reserva e o alerta
       seria reenviado a cada 5 minutos para sempre."""
        fila.enqueue(_ev(recurso="a"), ready_at=T0, renotify_minutes=0, now=T0)
        entregues = fila.due(T0)
        assert [i.resource_id for i in entregues] == ["a"]

        fila.mark_sent(entregues[0].id, T0)

        assert fila.due(T0) == []
        assert fila.due(T0 + timedelta(seconds=LEASE_SEGUNDOS + 1)) == [], (
            "enviado nao volta nem com a reserva expirada"
        )
        assert fila.stats(T0)["pending"] == 0

    def test_item_novo_ainda_sai_enquanto_outro_esta_reservado(self, fila) -> None:  # noqa: ANN001
        """A reserva e por ITEM, nao um bloqueio geral da fila."""
        fila.enqueue(_ev(recurso="a"), ready_at=T0, renotify_minutes=0, now=T0)
        assert [i.resource_id for i in fila.due(T0)] == ["a"]

        fila.enqueue(_ev(recurso="b"), ready_at=T0, renotify_minutes=0, now=T0)
        assert [i.resource_id for i in fila.due(T0)] == ["b"], (
            "o item novo precisa sair; a reserva do 'a' nao pode travar a fila"
        )


class TestEnqueueResisteACorrida:
    """`ON CONFLICT (dedup_key) WHERE status = 'pending' DO NOTHING`.

    Sem isso, dois enqueues concorrentes da mesma chave de estado caem os dois no
    INSERT: um levanta `UniqueViolation`, que sobe ate `EventPipeline.enqueue` e
    e engolido por `main._laco_do_job`. O evento fica gravado em `event` e
    NUNCA e enfileirado -- a constraint convertia corrida em perda de dado em
    vez de retry.
    """

    def test_o_insert_tem_on_conflict_no_indice_parcial(self, fila) -> None:  # noqa: ANN001
        """Verificacao estatica do SQL, nao da corrida.

        Uma corrida real entre duas transacoes depende de temporizacao e nao e
        um teste confiavel -- ela passaria 999 vezes e falharia na fazenda.
        O que dá para garantir e que a clausula existe e mira o indice certo:
        `ux_queue_pendente_por_chave` (migration 004), que e UNIQUE parcial
        sobre `status = 'pending'`. Sem o predicado no `ON CONFLICT`, o
        Postgres nao infere o indice e o INSERT volta a levantar.
        """
        import inspect

        from agent_nio_monitor.store.postgres_queue import PostgresQueue

        sql = inspect.getsource(PostgresQueue.enqueue)
        assert "ON CONFLICT (dedup_key) WHERE status = 'pending' DO NOTHING" in sql, (
            "o INSERT precisa de ON CONFLICT com o predicado do indice parcial "
            "`ux_queue_pendente_por_chave`; sem ele a corrida vira perda de dado"
        )
        assert ".rowcount" in sql, (
            "e o rowcount que distingue ENFILEIRADO de ATUALIZADO sem excecao -- "
            "e o que devolve o resultado VERDADEIRO a quem perdeu a corrida"
        )

    def test_pendente_existente_faz_atualizar_e_nao_duplicar(self, fila) -> None:  # noqa: ANN001
        """O efeito observavel do ON CONFLICT, sem depender de corrida."""
        assert fila.enqueue(_ev(recurso="w"), ready_at=T0, renotify_minutes=0, now=T0) == ENFILEIRADO
        assert fila.enqueue(_ev(recurso="w"), ready_at=T0, renotify_minutes=0, now=T0) == ATUALIZADO

        with fila.db.connection() as conn:
            total = conn.execute(
                "SELECT count(*) AS n FROM hermes.notification_queue WHERE dedup_key = %s",
                ("container_stopped:w",),
            ).fetchone()["n"]
            conn.commit()
        assert total == 1, f"{total} linhas para a mesma chave; o indice parcial nao valeu"


