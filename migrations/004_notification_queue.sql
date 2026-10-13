-- 004 — fila interna de disparo de notificacoes
--
-- O alerta nao sai mais no instante da deteccao: entra aqui e, a cada ciclo, o
-- agente reconfere se o problema continua. Resolvido = descarta; persistente =
-- envia UMA vez. A fila e persistida para sobreviver a reinicio do agente.
--
-- Tabela pequena e de vida curta, entao NAO e particionada (as de serie temporal
-- sao). Linhas encerradas sao removidas pelo proprio agente depois de 30 dias.

CREATE TABLE IF NOT EXISTS hermes.notification_queue (
    id               UUID        PRIMARY KEY,
    event_id         UUID        NOT NULL,
    dedup_key        TEXT        NOT NULL,
    event_type       TEXT        NOT NULL,
    resource_id      TEXT,
    status           TEXT        NOT NULL DEFAULT 'pending',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    ready_at         TIMESTAMPTZ NOT NULL,
    next_attempt_at  TIMESTAMPTZ,
    sent_at          TIMESTAMPTZ,
    attempts         INTEGER     NOT NULL DEFAULT 0,
    last_error       TEXT,
    resolved_reason  TEXT,
    payload          JSONB       NOT NULL,
    CONSTRAINT ck_queue_status_conhecido
        CHECK (status IN ('pending', 'sent', 'discarded', 'failed'))
);

-- Anti-duplicidade: no maximo UM item pendente por chave.
CREATE UNIQUE INDEX IF NOT EXISTS ux_queue_pendente_por_chave
    ON hermes.notification_queue (dedup_key) WHERE status = 'pending';

-- A consulta do despachante: pendentes cujo prazo ja chegou.
CREATE INDEX IF NOT EXISTS ix_queue_pendentes
    ON hermes.notification_queue (ready_at) WHERE status = 'pending';

-- Janela de re-notificacao: ultimo envio de uma chave.
CREATE INDEX IF NOT EXISTS ix_queue_enviados
    ON hermes.notification_queue (dedup_key, sent_at DESC) WHERE status = 'sent';
