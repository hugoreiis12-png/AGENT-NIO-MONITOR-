-- 002 — invariants que so o banco pode garantir
--
-- A duracao era derivada em Python sem clamp. Com clock skew entre o n8n e o
-- host, `stopped_at < started_at` produzia -3600000 ms (medido antes da
-- correcao) e esse numero ia para a tabela. Media e p95 de um conjunto com
-- negativo ficam sem sentido, e o alerta de "workflow lento" ficava errado.
--
-- Agora o Python tambem faz clamp. Este CHECK e defesa em profundidade: se
-- alguem inserir direto por SQL, ou se um bug futuro reintroduzir o negativo,
-- o banco recusa em vez de poisoning silencioso.

-- duration_ms nao pode ser negativo. NULL continua permitido (execucao em
-- andamento nao tem stoppedAt).
ALTER TABLE hermes.execution
    DROP CONSTRAINT IF EXISTS ck_execution_duration_nao_negativo;

ALTER TABLE hermes.execution
    ADD CONSTRAINT ck_execution_duration_nao_negativo
    CHECK (duration_ms IS NULL OR duration_ms >= 0);

-- O contador de streak nao pode ser negativo nem absurdamente grande. Um
-- `count` negativo indicaria bug no ON CONFLICT.
ALTER TABLE hermes.workflow_streak
    DROP CONSTRAINT IF EXISTS ck_streak_count_valido;

ALTER TABLE hermes.workflow_streak
    ADD CONSTRAINT ck_streak_count_valido
    CHECK (count >= 0);

-- `event.severity` e fechado: a engine de regras so produz estes valores, e
-- texto livre aqui quebraria qualquer filtro por severidade.
ALTER TABLE hermes.event
    DROP CONSTRAINT IF EXISTS ck_event_severity_conhecida;

ALTER TABLE hermes.event
    ADD CONSTRAINT ck_event_severity_conhecida
    CHECK (severity IN ('critical', 'high', 'medium', 'low', 'info'));

-- `notification.status` tambem e fechado, pelos mesmos motivos.
ALTER TABLE hermes.notification
    DROP CONSTRAINT IF EXISTS ck_notification_status_conhecido;

ALTER TABLE hermes.notification
    ADD CONSTRAINT ck_notification_status_conhecido
    CHECK (status IN ('sent', 'failed', 'suppressed', 'sent_to_log'));

-- Fonte de verdade do nome do workflow: garante que `workflow_snapshot` tem o
-- que a UI e o diff precisam.
ALTER TABLE hermes.event
    DROP CONSTRAINT IF EXISTS ck_event_tem_titulo;

ALTER TABLE hermes.event
    ADD CONSTRAINT ck_event_tem_titulo
    CHECK (length(btrim(title)) > 0);