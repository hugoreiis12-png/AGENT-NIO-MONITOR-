# ADRs — por que o desenho é este

Formato: decisão · alternativas descartadas · por quê · trade-off aceito · como reverte.

---

## D1 — Postgres, schema separado

**Decisão:** Postgres, schema `hermes`, nunca o database do n8n.
**Descartado:** JSON Lines + arquivos JSON (proposta original), SQLite.
**Por quê:** o documento original pedia agregação de série temporal — restarts em janela de 1 h, p95 de duração, `mean × 3` de spike. Com `jsonl` isso significa carregar tudo em memória a cada ciclo. Medido na instância: ~540 execuções/dia, das quais ~24 falham. O volume real está em `execution_failure` (~4 GB/ano), não no metadado (~40 MB/ano).
**Trade-off:** operação de partições. Mitigado por `DROP PARTITION`, nunca `DELETE` em massa.
**Reverte:** `DROP SCHEMA hermes CASCADE` e a migração é o único caminho de escrita.

---

## D2 — Nunca assumir versão de API; sondar no boot

**Decisão:** `capabilities.py` sonda cada endpoint no boot, grava em `capabilities` e adapta.
**Descartado:** fixar versão no código.
**Por quê:** durante o planejamento eu afirmei, com base no `openapi.yml` da tag `n8n@1.102.4`, que `/workflows/{id}/history` e `authors` **não existiam**. A instância real respondeu com `activeVersion` e `authors` preenchido (`nio@grupocampanha.com.br`). A premissa estava errada e tinha entrado no plano. Se ela tivesse chegado ao código, teríamos perdido atribuição sem perceber.
**Trade-off:** um probe a cada start.
**Reverte:** remover `capabilities.py` e fixar versão.

> Comando que responde a pergunta: `python -m agent_nio_monitor.main doctor`

---

## D3 — O LLM redige; a regra decide

**Decisão:** o LLM (Qwen3.8-27B no vLLM local; antes `hermes3:8b`) analisa o erro e redige resumo, causa provável e passos para resolver. Severidade, destinatário, dedup e quiet-hours são regras determinísticas.
**Descartado:** LLM decide criticidade.
**Por quê:** um 8B Q4 (4,34 GB) é não-determinístico e frágil em raciocínio multi-passo. Decisão de alerta não pode depender de caixa-preta. Além disso o fallback precisa ser completo: se o LLM cair, o alerta tem que sair igual.
**Trade-off:** o texto pode ser medíocre. Aceito — o requisito é "direto", e o modelo entrega.
**Verificado:** latência real de 3,76 s. Teste `test_latencia_ficou_dentro_do_aceitavel` trava 30 s como limite.

---

## D4 — Evento é streak, não execução

**Decisão:** "workflow X falhou 9 vezes desde 11/10 16:30" é **um** evento.
**Descartado:** um evento por execução (o documento original).
**Por quê:** medido nos 25 erros mais recentes — 8 workflows distintos, com os 3 maiores = **76%** do total, e em horários determinísticos (`JqOi9bhDIXzi8wKN` às `:30` de cada hora). Isso não são 25 incidentes: são 8 jobs quebrados.
**Trade-off:** o alerta não chega no instante exato da 2ª falha se a 1ª já foi notificada. Compensado pela janela de 12 h por regra.
**Reverte:** emitir também o evento unitário; só não notificar.

> **Correção registrada:** a meta original de "25 erros → 5 emails" era aritmeticamente impossível — o piso é 8, um por workflow quebrado. O critério de aceite foi corrigido para piso 8 / teto 13, e a implementação entrega **11**.

---

## D5 — Redator na fronteira de serialização

**Decisão:** `redaction()` antes de qualquer escrita, envio ou prompt.
**Descartado:** redigir só no template de email.
**Por quê:** no `GET /workflows` real da sua instância há credencial de produção em texto puro — login SAP (`Password: jfc2025`) e duas conexões `dblink` (`user=postgres password=postgres`). Se o `runData` entrasse no banco de auditoria ou num prompt, o banco seria um cofre de credencial.
**Defesa em profundidade:** redige no coletor **e** no repositório. Três estratégias, porque os casos reais não se resolvem com uma:
1. por chave (`password`, `apiKey`)
2. por chave-irmã (`{"name": "Password", "value": "..."}` — `value` é inocente, quem nomeia o segredo é `name`)
3. por padrão em texto (`dblink('... password=...')` dentro de uma query SQL)
**Reverte:** remover chamadas — mas então o banco vaza.

---

## D6 — Cadências por custo assimétrico

**Decisão:** erros 60 s · estado de workflow 5 min · corpo completo 15 min · stats 5 min · reconciliação 1 h.
**Descartado:** polling uniforme a 30–60 s (o documento original).
**Por quê:** `GET /workflows` devolveu **11,2 MB** na sua instância. A 60 s seriam ~16 GB/dia de HTTP e parsing para descobrir que nada mudou. A 15 min, ~1 GB/dia em LAN.
**Trade-off:** uma mudança de conteúdo é detectada com até 15 min de atraso. Aceito — falha é detectada em 60 s, que é o que tem urgência.
**Reverte:** mexer em `environments.yaml`. O teste `test_cadencia_de_workflow_respeita_o_custo_medido` trava o piso de 600 s.

---

## D7 — Cursor inválido é buraco, não erro

**Decisão:** cursor que o n8n invalidou (por prune) gera evento `reconciliation_gap`, rescan do zero, e cursor substituído.
**Descartado:** "tentar a mesma cursor de novo".
**Por quê:** a execução do documento original promete "armazenar todo histórico de execuções". Se o Hermes ficar parado mais que `EXECUTIONS_DATA_*_MAX_AGE`, a cursor aponta para registro removido e os eventos desse intervalo somem **em silêncio**. Um buraco silencioso é pior que uma lacuna declarada.
**Detalhe discovered pelos testes:** algumas versões respondem **400** em vez de 404 para cursor inválida. Sem diferenciar, vira erro transitório e o buraco nunca é reportado. O cliente agora distingue por conteúdo do corpo (`CursorInvalid`) — e trata 400 sem menção a cursor como erro normal, para não gerar alarme falso.
**Reverte:** `collector.py` / `client.py`.

---

## D8 — O canal de saída não é o que monitora

**Decisão:** n8n de pé → webhook do n8n. n8n fora → **log em stderr** com prefixo `HERMES-ALERTA`.
**Descartado:** (implícito no documento original) depender do n8n para tudo.
**Por quê:** o Hermes envia email pelo n8n que monitora. Quando o n8n cai — que é justamente o evento crítico — o canal de alerta morre junto. É falha correlacionada, e é o defeito de disponibilidade mais grave do desenho original.
**Trade-off aceito pelo dono:** no cenário de n8n fora, **não há email**. O alerta chega a alguém só se algo consumir esse log.
**Consequência explícita:** o dead-man's switch não é opcional. Monitor que falha em silêncio precisa de alguém de fora que note — ver `docs/runbook.md`.
---

## D9 — Insercao em lote com unnest + ON CONFLICT

**Decisao:** `INSERT ... SELECT FROM unnest(ARRAY[...]) ON CONFLICT DO NOTHING`, um lote por coleta.
**Descartado:** `COPY ... FROM STDIN`.
**Por que:** a versao com COPY estava quebrada em tres pontos e nunca foi executada. `cursor.copy()` no psycopg3 tem assinatura `(statement, params, *, writer)` — `columns="row"` nao existe. `cur.executemany()` nao alimenta o COPY. E COPY nao tem `ON CONFLICT`, entao reprocessar a mesma pagina (o que acontece depois de um reset de cursor) derrubava a transacao com `UniqueViolation`. Usei `columns="row"` de memoria do `copy_expert` do psycopg2 sem confirmar a versao — exatamente o que a skill proibe.
**Verificado:** a forma `unnest(ARRAY[...], ARRAY[...], ...) AS t(...)` foi executada no PostgreSQL 16.4 (192.168.0.250) antes de ser adotada.
**Trade-off:** um statement em vez de streaming. Com ~540 execucoes/dia e lotes de 25, irrelevante.
**Reverte:** `postgres_repository.record_executions`.

---

## D10 — Least privilege: dois databases, nao um schema

**Decisao:** `hermes_app` (NOSUPERUSER, NOCREATEDB, CONNECTION LIMIT 20) com acesso apenas a `hermes` e `hermes_test`.
**Descartado:** schema `hermes` dentro de `postgres`; reuso do role `postgres`.
**Por que (verificado em 192.168.0.250):** o servidor tem **49 databases de negocio** e **todas as conexoes existentes usam o role `postgres`** — inclusive 48 conexoes ociosas de `intranet-jfc` vindas de 192.168.0.160. Um superusuario com acesso a `FABRICA` e `DB-COMERCIAL` rodando um processo com retry e polling e risco operacional concreto.
**Trade-off:** 1 operacao manual de CREATE DATABASE/ROLE, e a migration precisa de role de infraestrutura.
**Reverte:** `DROP DATABASE hermes; DROP DATABASE hermes_test; DROP ROLE hermes_app;`
**Risco separado, fora de escopo:** as 48 conexoes ociosas de `intranet-jfc` sao pool aberto num servidor com `max_connections=300`. Registrar, nao corrigir aqui.

---

## D11 — O bootstrap SQL fica FORA de migrations/

**Decisao:** `sql/00_criar_banco.sql` e aplicado por humano; `migrations/` so tem DDL de schema.
**Por que:** `Database.migrate()` le `migrations/*.sql` no boot do agente. Com o bootstrap junto, o processo tentaria `CREATE ROLE` e `CREATE DATABASE` sozinho, sem revisao, num servidor de producao. O teste `test_bootstrap_sql_fora_de_migrations` trava isso.
**Alternativa descartada:** distinguir por prefixo de nome do arquivo — depende de convencao, falha em silencio.
**Reverte:** mover o arquivo e remover o teste (nao recomendado).

---

## D12 — Retentativa em 5xx, nao so em erro de rede

**Decisao:** `_deve_retryar` repete em timeout/rede, **5xx e 429**; nunca em 4xx.
**Descartado:** repetir em tudo (gasta retry em `CursorInvalid`, que precisa de reset e nao de insistencia).
**Por que:** durante restart do n8n o gateway responde 502/503 — e exatamente quando insistir importa. A versao anterior so repetia em `TimeoutException`/`NetworkError`, entao um pico de 503 registrava erro e o agente passava 60 s as cegas, cego justamente no incidente.
**Cuidado:** `httpx.HTTPStatusError` tambem entra no predicado, porque `raise_for_status()` pode ser alcancado fora do tratamento explicito de status.
**Reverte:** `sources/n8n/client.py` e `sources/portainer/client.py`.

---

## D13 — Redator cobre termos em portugues

**Decisao:** `senha`, `segredo`, `chave` (match exato), `privada` entraram como chaves e tokens sensiveis.
**Por que:** achado por `tests/unit/test_bordas.py` — `{"senha": "jfc2025"}` **nao era redigido**. A lista so tinha termos em ingles, num ambiente cujo stack e SAP e cujos workflows usam `senha` como nome de campo.
**Deliberadamente fora:** `chave` como token. `chaveDeOrdem`, `chavePrimaria`, `chavePix` sao de negocio; redigi-los destruiria o diagnostico. `chavePrivada` e redigido pelo token `privada`.
**Trade-off:** um payload a mais para revisar, e o redator ainda pode deixar passar forma de segredo que nao existe no dicionario.

---

## D14 — Dois databases, UM schema (`hermes` em ambos)

**Decisao:** `hermes` e `hermes_test` sao databases separados, e a schema chama-se `hermes` nos dois.
**Descartado:** schema `hermes_test` para o banco de teste (o que as duas suites de integracao usavam, uma de cada jeito: uma pedia `public`, a outra `hermes_test`).
**Por que:** `Database(schema=)` so ajusta o `search_path` da conexao; a migration tem o nome fixo. Entao pedir schema diferente nao muda nada -- `TRUNCATE hermes_test.execution` apontava para tabela inexistente. Com um schema unico nos dois databases, o hardcode deixa de ser defeito e cada banco continua isolado para `DROP`.
**Verificado:** 3 particoes por tabela particionada, 31 objetos em `hermes`.
**Reverte:** nada -- e a forma como o DDL ja esta escrito.

---

## D15 — Criar schema e infraestrutura, nao migration

**Decisao:** `CREATE SCHEMA` fica em `sql/00_criar_banco.sql`, fora de `migrations/`.
**Por que (verificado executando):** `CREATE SCHEMA`, mesmo com `IF NOT EXISTS`, exige `CREATE` no **DATABASE** -- privilegio muito mais largo que `CREATE` na schema. O agente tem so `CREATE` na schema `hermes`. Resultado: a migration 001 abortava inteira com `permissao negada para banco de dados`, e nenhuma tabela era criada.
**Trade-off:** rodar migration sem rodar o bootstrap antes da erro "relacao nao existe" -- mensagem clara, e o runbook cobre.
**Teste:** `test_migration_nao_cria_schema`.

---

## D16 — Limites de particao em UTC

**Decisao:** `part_start::timestamp AT TIME ZONE 'UTC'`.
**Descartado:** calcular no fuso da sessao; e `date::timestamptz AT TIME ZONE 'UTC'`.
**Por que:** limite de particao e instante absoluto. Calcular no fuso do servidor (aqui `America/Sao_Paulo`) faz o `TimeZone` do servidor deslocar as fronteiras. E `::timestamptz` usa o fuso da sessao ANTES do `AT TIME ZONE`, convertindo duas vezes: limite saia 03:00 em vez de 00:00.
**Teste:** `test_limites_em_utc_sem_conversao_dupla`.

---

## D17 — A tabela de particao e criada COM schema

**Decisao:** `CREATE TABLE IF NOT EXISTS hermes.<p>_<aaaamm> PARTITION OF hermes.<p>`.
**Por que:** sem o prefixo, o `CREATE` usa o primeiro schema do `search_path` da sessao. Na sessao de teste esse primeiro schema era `public`, onde o agente nao tem `CREATE` -- e a funcao falhava com `permissao negada para esquema public`. Este foi um bug que **eu introduzi** ao "corrigir" a funcao; o teste `test_tabela_criada_e_qualificada_com_schema` existe porque dele.
