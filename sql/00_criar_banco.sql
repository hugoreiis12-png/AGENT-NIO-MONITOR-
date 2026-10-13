-- ===========================================================================
-- AGENT NIO MONITOR — criacao do role e dos databases
-- ===========================================================================
--
-- PARA QUE DUAS BASES E NAO UMA
-- -------------------------------
-- Este servidor tem 49 databases de negocio e TODAS as conexoes existentes usam
-- o role `postgres` (superusuario) — inclusive 48 conexoes ociosas de
-- `intranet-jfc` vindas de 192.168.0.160. Um superusuario com acesso a tudo,
-- rodando um agente com polling e retry, e risco operacional concreto: um bug
-- de SQL pode tocar FABRICA ou DB-COMERCIAL.
--
-- Um schema dentro de uma base existente nao resolveria: o agente ainda
-- carregaria (ou nao) os grants daquela base, e um `DROP SCHEMA` errado num
-- schema compartilhado e desastre multiempresa.
--
-- DECISAO: dois databases, um role, sem acesso a nada alem deles.
-- Alternativa descartada: schema `hermes` dentro de `postgres`.
-- Reverte: DROP DATABASE hermes; DROP DATABASE hermes_test; DROP ROLE hermes_app;
--
-- COMO APLICAR
-- ------------
--   psql -h 192.168.0.250 -p 5432 -U postgres -d postgres -f 00_criar_banco.sql
--
-- Em SQL puro precisa ser executado com autocommit (psql faz por padrao).
-- A variavel `senha_gerada` tem que ser trocada antes.
--
-- ===========================================================================

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- 1. Role dedicado
-- ---------------------------------------------------------------------------
-- LOGIN, sem SUPERUSER, sem CREATEDB, sem CREATEROLE, sem BYPASSRLS.
-- Deliberadamente NAO-owner: o agente so precisa de CONNECT e CRUD no schema
-- proprio, e nao de criar objetos. O DDL e aplicado pelo role `postgres`
-- (owner), nao pelo agente.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hermes_app') THEN
        CREATE ROLE hermes_app LOGIN;
    END IF;
END
$$;

-- Reforco explicito: se o role ja existisse com privilegios a mais, isto limpa.
ALTER ROLE hermes_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
ALTER ROLE hermes_app CONNECTION LIMIT 20;

-- ---------------------------------------------------------------------------
-- ---------------------------------------------------------------------------
-- 2. Schema e privilegios (REPETIDO em cada database)
-- ---------------------------------------------------------------------------
-- Decisao: DOIS databases, UM schema -- `hermes` nos dois. Entao o nome do schema
-- pode ser fixo no SQL da migration sem virar defeito.
--
-- Motivo do CREATE para hermes_app: as tabelas vivem na schema `hermes`, e o role
-- so tinha CONNECT. Sem isto o agente conecta e nao le nem escreve nada.
--
-- CREATE (nao so USAGE) porque as migrations rodam com o mesmo DSN dos testes --
-- um unico segredo para operar. O dano de um DROP acidental fica restrito ao
-- schema do agente, nao as 49 bases de negocio deste servidor.
--
-- Para endurecer depois: criar um role `hermes_migrate` com CREATE e deixar
-- `hermes_app` so com DML. Exige dois segredos: e o tradeoff entre isolamento e
-- operacional.

-- CONNECT e privilegio de NIVEL DATABASE, separado do schema. Sem esta linha a
-- conexao nem chega a happen -- o erro e "permissao negada para banco de dados",
-- que parece problema de senha e nao e.
GRANT CONNECT ON DATABASE hermes TO hermes_app;
GRANT CONNECT ON DATABASE hermes_test TO hermes_app;

\connect hermes
CREATE SCHEMA IF NOT EXISTS hermes;
REVOKE ALL ON SCHEMA hermes FROM PUBLIC;
REVOKE ALL ON SCHEMA hermes FROM hermes_app;
GRANT USAGE, CREATE ON SCHEMA hermes TO hermes_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA hermes
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hermes_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA hermes
    GRANT USAGE, SELECT ON SEQUENCES TO hermes_app;

\connect hermes_test
CREATE SCHEMA IF NOT EXISTS hermes;
REVOKE ALL ON SCHEMA hermes FROM PUBLIC;
REVOKE ALL ON SCHEMA hermes FROM hermes_app;
GRANT USAGE, CREATE ON SCHEMA hermes TO hermes_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA hermes
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hermes_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA hermes
    GRANT USAGE, SELECT ON SEQUENCES TO hermes_app;

-- ---------------------------------------------------------------------------
-- 3. Verificacao
------------------------
\connect postgres

SELECT datname AS database,
       pg_get_userbyid(datdba) AS owner
  FROM pg_database
 WHERE datname LIKE 'hermes%'
 ORDER BY datname;

SELECT rolname,
       rolsuper AS superusuario,
       rolcreatedb AS cria_db,
       rolcanlogin AS pode_logar,
       rolconnlimit AS limite_conexoes
  FROM pg_roles
 WHERE rolname = 'hermes_app';

-- Resultado esperado:
--   hermes      | postgres
--   hermes_test | postgres
--   hermes_app  | f | f | t | 20

-- NOTA DE SEGURANCA
-- A senha do role NAO fica neste arquivo. Este arquivo e versionavel.
-- Depois de criar o role, defina a senha na mao, fora do versionado:
--
--   \psql -h 192.168.0.250 -p 5432 -U postgres -d postgres \
--     -c "ALTER ROLE hermes_app PASSWORD '<senha-forte>'"
--
-- E grave o DSN completo no `.env` (gitignored):
--
--   DATABASE_DSN=postgresql://hermes_app:<senha>@192.168.0.250:5432/hermes_test
