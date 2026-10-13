-- AGENT NIO MONITOR -- schema inicial
--
-- Retencao por DROP PARTITION, nunca DELETE em massa: em tabela particionada o
-- DELETE nao libera espaco e trava. Em tabela nao particionada, DELETE em massa
-- sobre ~200k linhas/ano trava e faz o vacuum_marker andar por semanas.
--
-- Schema separado do n8n. Nunca no mesmo database.
--
-- O schema `hermes` NAO e criado aqui: isso e responsabilidade de infraestrutura
-- e vive em `sql/00_criar_banco.sql`. Motivo concreto: `CREATE SCHEMA`, mesmo com
-- `IF NOT EXISTS`, exige CREATE no DATABASE -- privilegio bem mais largo que
-- CREATE na schema. Rodando a migration com um role que tem CREATE apenas na
-- schema `hermes` (o menor privilegio que queremos), o `CREATE SCHEMA` falha
-- com "permissao negada para banco de dados" e a migration inteira aborta.
-- Verificado executando.
--
-- Aplicar `sql/00_criar_banco.sql` ANTES deste arquivo.

SET search_path TO hermes, public;

-- ===========================================================================
-- Estado operacional (nao particionado: sempre 1 linha por coisa)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS source_cursor (
    source           TEXT        NOT NULL,
    scope            TEXT        NOT NULL,      -- 'errors' | 'workflows' | 'containers'
    cursor           TEXT,
    last_success_at  TIMESTAMPTZ,
    last_attempt_at  TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    detail           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (source, scope)
);

CREATE TABLE IF NOT EXISTS capabilities (
    source       TEXT        NOT NULL,
    feature      TEXT        NOT NULL,
    path         TEXT        NOT NULL,
    available    BOOLEAN     NOT NULL,
    status_code  INTEGER,
    detail       TEXT,
    probed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, feature, path)
);

CREATE TABLE IF NOT EXISTS resource (
    source      TEXT        NOT NULL,          -- 'n8n' | 'portainer'
    kind        TEXT        NOT NULL,          -- 'workflow' | 'credential' | 'project' | 'stack' | 'container'
    external_id TEXT        NOT NULL,
    tenant_id   TEXT,                          -- projectId no n8n
    name        TEXT,
    critical    BOOLEAN     NOT NULL DEFAULT FALSE,
    meta        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    missing_since TIMESTAMPTZ,
    PRIMARY KEY (source, kind, external_id)
);

CREATE INDEX IF NOT EXISTS ix_resource_tenant ON resource (tenant_id);
CREATE INDEX IF NOT EXISTS ix_resource_missing ON resource (missing_since) WHERE missing_since IS NOT NULL;

-- Contador de falha por workflow. E a base do D4: o evento que aciona e
-- "X falhou N vezes desde T", nao "a execucao Y falhou".
CREATE TABLE IF NOT EXISTS workflow_streak (
    workflow_id       TEXT        PRIMARY KEY,
    tenant_id         TEXT,
    workflow_name     TEXT,
    count             INTEGER     NOT NULL DEFAULT 0,
    first_at          TIMESTAMPTZ,
    last_at           TIMESTAMPTZ,
    last_notified_at  TIMESTAMPTZ,
    resolved_at       TIMESTAMPTZ
);

-- ===========================================================================
-- Serie temporal (particionado por mes)
-- ===========================================================================

-- Metadados de TODA execucao. Medido: ~540/dia -> ~197k/ano -> ~40 MB/ano.
CREATE TABLE IF NOT EXISTS execution (
    started_at   TIMESTAMPTZ NOT NULL,
    id           BIGINT      NOT NULL,
    tenant_id    TEXT,
    workflow_id  TEXT        NOT NULL,
    workflow_name TEXT,
    status       TEXT        NOT NULL,
    mode         TEXT,
    stopped_at   TIMESTAMPTZ,
    duration_ms  INTEGER,
    retry_of     TEXT,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, started_at)
) PARTITION BY RANGE (started_at);

-- O volume REAL. So das execucoes com falha (~24/dia), porque e o que serve para
-- auditoria e diagnostico. Sucesso nao precisa de payload.
CREATE TABLE IF NOT EXISTS execution_failure (
    started_at   TIMESTAMPTZ NOT NULL,
    id           BIGINT      NOT NULL,
    workflow_id  TEXT        NOT NULL,
    tenant_id    TEXT,
    error_msg    TEXT,
    last_node    TEXT,
    run_data     JSONB,
    truncated    BOOLEAN     NOT NULL DEFAULT FALSE,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, started_at)
) PARTITION BY RANGE (started_at);

CREATE TABLE IF NOT EXISTS event (
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    id            UUID        NOT NULL,
    tenant_id     TEXT,
    source        TEXT        NOT NULL,
    resource_kind TEXT,
    resource_id   TEXT,
    event_type    TEXT        NOT NULL,
    severity      TEXT        NOT NULL,
    title         TEXT        NOT NULL,
    detail        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    streak_count  INTEGER     NOT NULL DEFAULT 1,
    notified_at   TIMESTAMPTZ,
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

CREATE TABLE IF NOT EXISTS event_change (
    event_id UUID NOT NULL,
    seq      INTEGER NOT NULL,
    kind     TEXT    NOT NULL,   -- node_added | node_removed | node_changed | conn_added | conn_removed | settings_changed
    path     TEXT    NOT NULL,
    before   JSONB,
    after    JSONB,
    PRIMARY KEY (event_id, seq)
);

CREATE TABLE IF NOT EXISTS workflow_snapshot (
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    workflow_id TEXT        NOT NULL,
    version_id  TEXT,
    active      BOOLEAN     NOT NULL DEFAULT FALSE,
    body        JSONB       NOT NULL,        -- ja redigido, sempre
    PRIMARY KEY (workflow_id, captured_at)
) PARTITION BY RANGE (captured_at);

CREATE TABLE IF NOT EXISTS container_observation (
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    container_id  TEXT        NOT NULL,
    tenant_id     TEXT,
    state         TEXT,
    health        TEXT,
    restart_count INTEGER,
    mem_pct       NUMERIC(6,2),
    cpu_pct       NUMERIC(6,2),
    PRIMARY KEY (container_id, ts)
) PARTITION BY RANGE (ts);

CREATE TABLE IF NOT EXISTS notification (
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    id         UUID        NOT NULL,
    event_id   UUID,
    channel    TEXT        NOT NULL,
    recipients TEXT[]      NOT NULL,
    subject    TEXT        NOT NULL,
    body       TEXT,
    status     TEXT        NOT NULL,         -- sent | failed | suppressed_quiet_hours | sent_to_log
    error      TEXT,
    attempts   INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

CREATE TABLE IF NOT EXISTS reconciliation_run (
    run_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    source          TEXT        NOT NULL,
    scope           TEXT        NOT NULL,
    expected        INTEGER     NOT NULL DEFAULT 0,
    seen            INTEGER     NOT NULL DEFAULT 0,
    gaps            INTEGER     NOT NULL DEFAULT 0,
    cursor_valid    BOOLEAN     NOT NULL DEFAULT TRUE,
    duration_ms     INTEGER,
    detail          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (source, scope, run_at)
);

-- ===========================================================================
-- Indices
-- ===========================================================================

-- BRIN em coluna de tempo e o certo para serie append-only: ~1% do tamanho de
-- um BTREE e range scan em particoes pruned. BTHERE por indice.
CREATE INDEX IF NOT EXISTS ix_execution_started ON execution USING BRIN (started_at);
CREATE INDEX IF NOT EXISTS ix_execution_failure_started ON execution_failure USING BRIN (started_at);
CREATE INDEX IF NOT EXISTS ix_event_ts ON event USING BRIN (ts);
CREATE INDEX IF NOT EXISTS ix_snapshot_ts ON workflow_snapshot USING BRIN (captured_at);
CREATE INDEX IF NOT EXISTS ix_container_obs_ts ON container_observation USING BRIN (ts);
CREATE INDEX IF NOT EXISTS ix_notification_ts ON notification USING BRIN (ts);

-- Indice parcial: e exatamente a consulta que todo relatorio de falha faz.
CREATE INDEX IF NOT EXISTS ix_execution_failed
    ON execution (workflow_id, started_at DESC)
    WHERE status <> 'success';

CREATE INDEX IF NOT EXISTS ix_event_tenant_ts ON event (tenant_id, ts DESC);
CREATE INDEX IF NOT EXISTS ix_failure_workflow ON execution_failure (workflow_id, started_at DESC);

-- ===========================================================================
-- Gestao de particoes
-- ===========================================================================

CREATE OR REPLACE FUNCTION hermes.ensure_partitions(p_months_ahead INT DEFAULT 2)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE
    parent     TEXT;
    base_mes   DATE;
    part_start DATE;
    part_end   DATE;
    suffix     TEXT;
BEGIN
    -- Base em UTC, nao no fuso da sessao.
    --
    -- Motivo: os limites da particao sao instantes absolutos. Se forem calculados
    -- no fuso do servidor (`America/Sao_Paulo`), mudar o TimeZone do servidor
    -- desloca as fronteiras e cria buraco de mes silencioso -- exatamente o que
    -- aconteceu na primeira versao, que produzia `execution_2026_09`,
    -- `execution_2026_11`, `execution_2027_02` e pulava outubro.
    base_mes := (date_trunc('month', now() AT TIME ZONE 'UTC'))::DATE
                - INTERVAL '1 month';

    FOREACH parent IN ARRAY ARRAY[
        'execution', 'execution_failure', 'event',
        'workflow_snapshot', 'container_observation', 'notification'
    ] LOOP
        -- `part_start`/`part_end` sao RECALCULADOS a partir de `base_mes` a cada
        -- volta. A versao anterior mutava `start_d` dentro do laco E ainda somava
        -- `i * INTERVAL '1 month'`, contando a mesma linha duas vezes e deixando
        -- o mes corrente sem particao.
        FOR i IN 0..p_months_ahead LOOP
            part_start := (base_mes + i * INTERVAL '1 month')::DATE;
            part_end   := (base_mes + (i + 1) * INTERVAL '1 month')::DATE;
            suffix     := to_char(part_start, 'YYYY_MM');
            EXECUTE format(
                -- Tabela QUALIFICADA com schema: sem o prefixo, o CREATE usa o
                -- primeiro schema do search_path e falha com "permissao negada
                -- para esquema public" quando a sessao nao comeca em hermes.
                'CREATE TABLE IF NOT EXISTS %I.%I PARTITION OF %I.%I '
                'FOR VALUES FROM (%L) TO (%L)',
                'hermes', parent || '_' || suffix,
                'hermes', parent,
                part_start::timestamp AT TIME ZONE 'UTC',
                part_end::timestamp   AT TIME ZONE 'UTC'
            );
        END LOOP;
    END LOOP;
END;
$$;

-- Cria as particoes. Idempotente.
SELECT hermes.ensure_partitions(2);

-- ===========================================================================
-- Retencao: DROP PARTITION, nunca DELETE
-- ===========================================================================

CREATE OR REPLACE FUNCTION hermes.drop_partitions_older_than(p_days INT)
RETURNS TABLE (parent TEXT, partition TEXT) LANGUAGE plpgsql AS $$
DECLARE
    r       RECORD;
    cutoff  DATE := (now() - (p_days || ' days')::INTERVAL)::DATE;
BEGIN
    FOR r IN
        SELECT c.relname AS part,
               split_part(c.relname, '_', 1) AS owner_table
        FROM pg_class c
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'hermes'
          AND c.relname ~ '_\d{4}_\d{2}$'
          AND to_date(right(c.relname, 7), 'YYYY_MM') < date_trunc('month', cutoff)
    LOOP
        EXECUTE format('DROP TABLE IF EXISTS hermes.%I', r.part);
        parent := r.owner_table;
        partition := r.part;
        RETURN NEXT;
    END LOOP;
END;
$$;