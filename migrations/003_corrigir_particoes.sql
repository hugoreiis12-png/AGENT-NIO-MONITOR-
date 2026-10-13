-- 003 — corrige o calculo de particoes
--
-- Sintoma observado em `hermes_test` apos a 001:
--
--   execution_2026_09   FOR VALUES FROM ('2026-09-01-03') TO ('2026-10-01-03')
--   execution_2026_11   FOR VALUES FROM ('2026-11-01-03') TO ('2026-12-01-03')
--   execution_2027_02   FOR VALUES FROM ('2027-02-01-03') TO ('2027-03-01-03')
--
-- Outubro AUSENTE. Qualquer INSERT de outubro falhava com
-- `CheckViolation: nenhuma particao da relacao "execution" encontrada`.
--
-- Duas causas na 001:
--
--  1. No laco, `start_d` era mutado (`start_d := end_d`) E ainda recebia
--     `i * INTERVAL '1 month'` na mesma expressao. A mesma linha era contada
--     duas vezes, produzindo um mes a cada dois.
--  2. Os limites usavam o fuso da sessao (America/Sao_Paulo). Limite de particao
--     e instante absoluto; mudar o TimeZone do servidor desloca as fronteiras.
--
-- A 001 tambem foi corrigida (para instalacao nova). Esta 003 cobre quem JA rodou
-- a versao antiga -- migration aplicada nao se edita.

CREATE OR REPLACE FUNCTION hermes.ensure_partitions(p_months_ahead INT DEFAULT 2)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE
    parent     TEXT;
    base_mes   DATE;
    part_start DATE;
    part_end   DATE;
    suffix     TEXT;
BEGIN
    -- Base em UTC: limites de particao sao instantes absolutos.
    base_mes := (date_trunc('month', now() AT TIME ZONE 'UTC'))::DATE
                - INTERVAL '1 month';

    FOREACH parent IN ARRAY ARRAY[
        'execution', 'execution_failure', 'event',
        'workflow_snapshot', 'container_observation', 'notification'
    ] LOOP
        -- `part_start`/`part_end` sao recalculados de `base_mes` a cada volta.
        -- Nao mutar `base_mes` dentro do laco.
        FOR i IN 0..p_months_ahead LOOP
            part_start := (base_mes + i * INTERVAL '1 month')::DATE;
            part_end   := (base_mes + (i + 1) * INTERVAL '1 month')::DATE;
            suffix     := to_char(part_start, 'YYYY_MM');
            EXECUTE format(
                -- TABELA QUALIFICADA COM SCHEMA. Sem o prefixo `hermes.`, o
                -- CREATE usa o primeiro schema do search_path -- que na sessao do
                -- teste e `public` -- e falha com "permissao negada para esquema
                -- public". Foi exatamente o erro observado.
                'CREATE TABLE IF NOT EXISTS %I.%I PARTITION OF %I.%I '
                'FOR VALUES FROM (%L) TO (%L)',
                'hermes', parent || '_' || suffix,
                'hermes', parent,
                -- `::timestamp AT TIME ZONE 'UTC'` = meia-noite UTC. A forma
                -- `::timestamptz AT TIME ZONE 'UTC'` converte duas vezes e produz
                -- 03:00 em vez de 00:00.
                part_start::timestamp AT TIME ZONE 'UTC',
                part_end::timestamp   AT TIME ZONE 'UTC'
            );
        END LOOP;
    END LOOP;
END;
$$;

-- Limpa as particoes orfas deixadas pela versao com bug: existem apenas
-- `*_2026_09`, `*_2026_11` e `*_2027_02`, e os meses de dezembro/2026 e
-- janeiro/2027 ficaram sem cobertura.
--
-- Lista explicita, e nao heuristica por nome: decidir DROP de tabela por
-- comparacao de string em `pg_get_expr` e o tipo de coisa que apaga dado na
-- manha errada.
DROP TABLE IF EXISTS
    hermes.execution_2026_11,
    hermes.execution_2026_09,
    hermes.execution_2027_02,
    hermes.execution_failure_2026_11,
    hermes.execution_failure_2026_09,
    hermes.execution_failure_2027_02,
    hermes.event_2026_11,
    hermes.event_2026_09,
    hermes.event_2027_02,
    hermes.workflow_snapshot_2026_11,
    hermes.workflow_snapshot_2026_09,
    hermes.workflow_snapshot_2027_02,
    hermes.container_observation_2026_11,
    hermes.container_observation_2026_09,
    hermes.container_observation_2027_02,
    hermes.notification_2026_11,
    hermes.notification_2026_09,
    hermes.notification_2027_02;

-- Garante a cobertura do mes corrente e dos proximos. Idempotente.
SELECT hermes.ensure_partitions(2);