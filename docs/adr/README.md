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
**Reverte:** mexer em `environments.yaml`.

> **Revisado depois:** o dono baixou todas as coletas para 25–60 s (corpo completo a 60 s,
> ~16 GB/dia em LAN) para a edição de workflow chegar em até 1 min. O piso de 600 s e o teste
> que o travava saíram; `test_todo_ciclo_de_coleta_fica_entre_25s_e_1min` trava a faixa nova.

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

---

## Fase 1 (2026-10-14) - Astra da fiaao

Auditoria de arquitetura achou 11 nomes (eventos, capacidades, funcoes) presentes em configuracao, template, ADR ou docstring e **sem nenhum emissor em `src/`**. Entre eles, os tres que um monitor nao pode perder: buraco de reconciliacao, `n8n_up` e destinatario global.

Fase 1 liga seis. As decisoes que nao sao obvias:

---

## D18 - O teste de invariante deriva os emissores do CODIGO

**Decisao:** `tests/unit/test_config.py` deriva os `event_type` emitidos por `ast`, rastreando a CONSTRUCAO do `EventRecord` -- e nao uma lista literal.

**Descartado:** manter a lista literal anterior (era assim) e so acrescentar `reconciliation_gap`.

**Por que:** a lista literal era um teste que confirmava a si mesmo. Continha `reconciliation_gap` (que nao era emitido) e nao continha nenhum evento que o codigo emite. Os dois lados do invariante mentiam na mesma direcao, entao passava. Auditorias inteiras nao pegaram: o bug nao era o teste faltar, era o teste **mentir**.

**Trade-off:** o scanner precisa resolver indirecao (parametro repassado no Portainer, `IfExp` no toggle de workflow) e ignora docstring. `test_scanner_encontra_os_emissores_reais` fixa a lista esperada, porque um scanner que degrada para "nao acha nada" passa vazio.

**Reverte-se assim:** remover a classe de testes; nada em producao depende.

---

## D19 - `reconciliation_gap` e ESTADO, e o Agent o emite

**Decisao:** `reconciliation_gap` entrou em `EVENTOS_DE_ESTADO`; o emissor fica em `Agent`, antes do early-return de `collected == 0`.

**Descartado:** emitir no coletor; deixar como fato (chave de dedup com `evento.id`).

**Por que:** (a) `collector.py` ja declara que registra e o Agent decide; (b) um buraco que se repete a cada ciclo de coleta -- o prune do n8n leva dias para ser resolvido -- com chave de fato seria um e-mail por ciclo; (c) buraco com `collected == 0` e o caso **mais** silencioso, e o early-return o apagava.

**Pre-requisito duro:** o evento nao tem `tenant_id`, entao depende do fallback global (D29) para chegar a alguem. Sem D29, a correcao de D19 teria movido a perda de lugar, nao eliminado.

---

## D20 - O fallback global e o ultimo degrau, e valeu vazio por muito tempo

**Decisao:** ligar `people.py: global_fallback` ao `Agent`; o VALOR fica para o dono (resolvido em D29).

**Por que:** e um endereco de pessoa e o dono decide. O que o codigo precisava garantir nao era "quem recebe", e "ninguem em silencio nao passa sem ruido": antes o alerta morria em `mark_failed` sem nenhum sinal.

**Teste:** `test_agent_liga_o_fallback_global_do_people_yaml` existe porque `people.global_fallback()` sem o fio no `Agent` reproduz o bug original com mais codigo.

---

## D21 - `n8n_up` vem do CLIENT, e 4xx nao conta como queda

**Decisao:** `N8NClient.alive` e espelhado em `HealthState.n8n_up` pelo `Agent`, depois de cada ciclo que fala com n8n.

**Descartado:** derivar de `health.mark("n8n_executions", ok)`.

**Por que:** `WorkflowIndex.load()` engole excecao e so loga. Uma queda do n8n que so aparece ali passaria pelo `mark`. O client e a unica fonte de verdade sobre alcancabilidade.

**5xx sim, 4xx nao:** 401 e 403 sao credencial, nao queda. Confundir faria o agente parar de mandar e-mail por token expirado -- o alerta sumiria justo quando o n8n precisa de atencao. A politica reusa `_deve_retryar`: duas listas de status divergentes dariam o pior resultado (insiste e reporta de pe).

---

## D22 - Escuridao parcial e distinta de total

**Decisao:** `CollectorResult.failures` conta endpoints ilegiveis (parcial, `ok=True`); `error` so quando **nenhum** responde (503 no `/healthz`). `fontes_cego` dispara com **2** ciclos consecutivos.

**Por que 2:** um timeout unico acionaria o dead-man's switch e treinaria o operador a ignorar. `stale_sources` sozinho nao cobre escuridao total -- ele so olha `last_collect_at`, que nao e atualizado justamente quando a coleta falha.

**Verificado antes de decidir:** escuridao **nao** gera `container_removed` falso. O codigo nao chama `_processar_endpoint` quando a listagem falha, entao `LifecycleTracker.removidos()` nao roda e o contador de ausencia nao avanca. Trava em `test_endpoint_escuro.py`.

**Trade-off:** falha parcial fica em HTTP 200, visivel em `fontes_parciais`. Preferi a 200 a 503 para nao treinar o operador contra alarme de endpoint unico.

---

## D23 - A sonda de capacidade roda DEPOIS do health subir

**Decisao:** `Agent.run_capabilities()` como task propria em `_agenda`, depois de `HealthServer.start()`.

**Descartado:** sondar no construtor do `Agent` (bloquearia o `/healthz` ate 110 s, e o cron externo do runbook veria queda, nao espera).

**Sonda paralela:** 11 rotas x 10 s de timeout sao ate 110 s sequencial. `asyncio.Semaphore` LOCAL -- um semaforo de modulo se prende ao primeiro event loop e falha em um dos testes.

**`except` largo e deliberado:** se a sonda explodir, o agente continua coletando. O contrario converte "um alerta perdido" em "o monitor inteiro parado".

---

## D24 - Escalacao NAO e ligada, por decisao

**Decisao:** `Escalator` fica em `mailer.py`, com logica correta e testada, sem nenhum chamador. `escalate_after_minutes` continua declarado em `policy.yaml` e inerte.

**Por que nao ligar agora:** (1) e a unica capacidade que MANDA MAIS E-MAIL, e o projeto ja registra fadiga como risco; (2) o gatilho exige `escalated_to` persistido (migration); (3) **quem recebe o degrau 2 e uma decisao de produto ainda nao respondida.**

**Como fica visivel:** `tests/unit/test_escalacao.py::test_escalator_nao_e_instanciado_em_producao` varre `src/` por `ast` e FALHA se a escalacao for ligada. O mesmo tratamento dado a `reconciliation_gap` e `save_capabilities` -- as tres estavam assim, e as tres passaram por auditorias inteiras.

---

## D25 - A idempotencia do streak JA EXISTIA -- e a "correcao" foi revertida

**Decisao:** `bump_streak` segue `count + 1` incondicional. **Nenhuma coluna nova, nenhuma migration.**

**A auditoria afirmou:** `bump_streak` nao tinha chave de idempotencia; um replay de coleta contaria a mesma falha duas vezes e a regra `min_streak: 3` chegaria a `critical` sobre uma falha so.

**A afirmacao estava errada.** A guarda existe um nivel acima: `ExecutionErrorCollector._process` so adiciona a `novos` o que `execution_exists()` instancia de verificar.

**Tentativa e reversao:** adicionei `workflow_streak.last_counted_execution_id` com `WHERE ... IS DISTINCT FROM EXCLUDED...` e a migration 005. Duas medicoes contrariam o ganho:

1. A coluna so barra repeticao **CONSECUTIVA**. Replay de sequencia (10, 11, 12 repetido) somava **6** em vez de 3 -- a protecao existia onde nao havia e faltava onde havia.
2. Com `execution_id=None` dos dois lados, `NULL IS DISTINCT FROM NULL` da falso e o streak **NUNCA incrementava**. Isso so apareceu contra Postgres real; `pglast` aceitou os dois SQL sem reclamar.

Resultado: uma coluna, uma migration e uma sensacao de protecao -- sem protecao. Revertido.

**Descartado:** `record_failure` devolvendo `bool` ("foi novo?"). Seria correto e nao precisaria de migration, mas nao ha bug para corrigir: ajouter mecanismo sem falha nomeada e o que a propria regra do projeto proibe.

**Como fica protegido daqui para frente:** `tests/integration/test_postgres_live.py::TestStreakNaoInfla` fixa o comportamento real -- `bump_streak` NAO e idempotente, e `execution_exists` e quem e.

**Licao registrada:** `bump_streak` tem docstring que explica por que o `count + 1` e incondicional e por que a idempotencia NAO mora ali. Sem isso, a proxima leitura acha um `count + 1` sem chave e conclui que falta protecao.

---

## D26 - `due()` RESERVA a linha; `enqueue` absorve a corrida no banco

**Decisao:** dois mecanismos, nenhum exige migration.

**`due()` com lease, nao com status novo:** `UPDATE ... SET next_attempt_at = now + lease WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED LIMIT n) RETURNING *`. Antes era `SELECT *` puro: nada mudava, entao uma segunda leitura devolvia a mesma linha e mandava o mesmo e-mail duas vezes. Seguro so porque `main._agenda` mantem UM laco serializado.

**Por que lease e nao `status='sending'`:** exigiria mexer no CHECK de `notification_queue.status` (migration 004) e criaria uma TERCEIRA fonte de verdade sobre a fase do item. `next_attempt_at` ja significa "quando volta a ser elegivel". Efeito colateral boo: se o processo morre no meio do envio, o item volta sozinho quando o lease expira.

**`LEASE_SEGUNDOS = 300`**, acima do backoff maximo de reenvio (240 s) e do timeout do LLM (60 s) mais as 3 tentativas do webhook. Lease MENOR que o tempo de um envio faria um segundo despachante roubar o item em voo. Trado por teste.

**`enqueue` com `ON CONFLICT (dedup_key) WHERE status='pending' DO NOTHING`:** o indice parcial e UNIQUE, entao o predicado no `ON CONFLICT` e obrigatorio para o Postgres inferir o indice. Sem isso, dois enqueues concorrentes da mesma chave caem os dois no INSERT; um levanta `UniqueViolation`, sobe ate `EventPipeline.enqueue` e e engolido por `main._laco_do_job` -- o evento fica gravado em `event` e **NUNCA e enfileirado**. A constraint convertia corrida em perda de dado em vez de retry. O `rowcount` do INSERT distingue ENFILEIRADO de ATUALIZADO, entao quem perde a corrida recebe o resultado verdadeiro em vez de excecao.

**Descartado:** teste de corrida real com duas transacoes. Depende de temporizacao e passaria 999 vezes ate falhar na fazenda. No lugar, verificacao estatica do SQL mais o efeito observavel.

**Bug proprio pego no caminho:** o `return SUPRIMIDO` saia de dentro do `with` sem `commit()`, deixando a transacao para o pool. Funcionava por acaso porque a pool commita no `__exit__`; virar checkout puro viraria transacao vazada.

---

## D27 - O heartbeat roda em THREAD propria (medido, nao suposto)

**Decisao:** `HealthServer` sobe em uma thread com o proprio event loop. `HealthState` ganhou um `threading.Lock` e todos os contadores viraram metodos.

**Medicao que justifica (executada antes de decidir):** servidor real, cliente FORA do processo, store sincrono bloqueando o loop por 2,0 s.

| | antes | depois |
|---|---|---|
| controle, sem bloqueio | 56 ms - 200 | 82-113 ms - 200 |
| store de 2,0 s no loop | **30.026 ms - ReadTimeout** | **16-18 ms - 200** |

A requisicao que chegava durante o store nao ficava LENTA: ficava **INALCANCAVEL**. O traceback mostrava `AssertionError` em `BaseEventLoop._attach` -- o aiohttp nem conseguia anexar o socket. Com o `--max-time 10` do cron do runbook, isso e "HERMES-MORTO".

**Descartado: `asyncio.to_thread` em todas as chamadas de store.** Seria a correcao da CAUSA. Medido: **59 pontos de chamada em 10 arquivos** -- refactor largo, com risco real de introduzir a classe de bug que a propria regra do projeto proibe.

**O que a escolha NAO resolve:** o loop do agente ainda bloqueia e as coletagens atrasam. Isso nao e defeito escondido: e o que `collect_errors` e `fontes_cego` existem para dizer. Depois de dois ciclos sem coleta, o `/healthz` responde 503 e o operador ve "vivo mas nao coletando".

**Risco aceito:** `HealthState` compartilhado entre threads. Mitigado com lock em `snapshot()` e em todos os mutadores, e com `test_snapshot_consistente_sob_concorrencia` (3 escritores, 3 leitores).

**Armadilha de metodo de medicao, registrada:** cliente DENTRO do loop nao mede bloqueio -- ele fica preso junto. Foi o que aconteceu nas duas primeiras versoes da sonda, que deram "NAO CONFIRMADO" com 2,5 ms. Precisa ser thread separada, disparada por `threading.Event`, sem nenhum `await` entre o evento e o bloqueio.

---

## D28 - Despachante concorrente, com teto e tempo maximo por item

**Decisao:** `QueueDispatcher.tick()` processa ate 4 itens ao mesmo tempo, com `asyncio.wait_for` de 180 s por item.

**O defeito:** iteracao serial com `await` dentro. Por item, `motivo()` pode chamar a API do n8n e `dispatch()` espera o LLM (60 s) mais 3 tentativas de webhook. Com 50 itens prontos, um tick muito ultrapassa o intervalo da fila -- ela cresce em vez de drenar.

**Teto 4, alto de proposito:** cada item em voo pode segurar uma chamada de LLM e uma conexao HTTP.

**180 s por item, maior que o pior caso (45 s de webhook + 60 s de LLM):** cortar antes faria um item lento virar falha definitiva enquanto ainda trabalhava.

**Timeout entra pelo mesmo caminho da excecao** (`_reagendar`, com motivo proprio). Tratar timeout como falha imediata perderia o alerta; retentar e o risco de duplicata que o caminho de excecao ja corre.

**Corrigido junto: o motivo de expiracao.** Era fixo, `"expirado (agente ficou parado?)"` -- descreve UM motivo possivel e nao o que o codigo mede. Com o head-of-line resolvido, o mais comum e a fila crescendo mais rapido do que drena, e mandar o operador investigar "agente parado" aponta para a causa errada. Agora diz quantos minutos ficou na fila, qual o TTL, e loga `alerta_expirou_na_fila` em `error`.

**`return_exceptions=True`:** antes, uma exception inesperada SUBIA do `tick` e os itens seguintes ficavam sem marcador nenhum -- status `pending`, invisiveis.

---

## D29 - `people.yaml: global` preenchido; `people.yaml:projects` e o caminho REAL

**Decisao:** `global:` recebe Hugo Reis, Rafael Barboza e Carlos Henrique. Sao os MESMOS donos de projeto, de proposito -- o fallback e o time responsavel usado quando nao ha projeto a quem atribuir.

**Correcao de um diagnostico anterior:** a auditoria e o D20 disseram que `/api/v1/projects` falhava por falta de papel admin na chave. **Errado.** A resposta real da instancia, verificada nesta sessao:

```
HTTP 403 -- "Your license does not allow for feat:projectRole:admin."
```

E limitacao de LICENCA, nao de credencial. Consequencia pratica: o caminho pela API **nunca** funcionara nesta instancia, entao `people.yaml:projects` nao e o plano B -- e o caminho NORMAL.

**Por que isso importava mais do que parecia:** `reconciliation_gap` (D19) nao tem `tenant_id`. Sem `global`, ele era o evento mais importante do sistema nascendo sem destinatario. Com D29, ele sai.

**O que a fiação NAO entregava:** D20 ligou o codigo em marcoes, mas com a lista vazia o resultado era identico ao anterior. So agora a entrega e real. `test_projeto_desconhecido_agora_chega_alguem` fixa isso; `test_o_fallback_global_nao_esta_vazio` impede regressao para lista vazia -- que `test_config.py` nao pegaria (email invalido no YAML nao quebra YAML).

---

## D30 - `source_down` e RASTREIO, e `notify: false` e a unica resposta correta

**Decisao:** `Agent._detecta_queda_do_n8n()` emite `source_down` na TRANSICAO (para `False`), nunca a cada ciclo.

**Por que `notify: false` nao e omissao:** o canal de envio e o **webhook do proprio n8n**. Com o n8n fora, o canal esta fora junto -- nem com `notify: true` o alerta sairia por e-mail. Ele simplesmente nunca chegaria a ninguem. Logo o valor do evento e o **rastro**: a queda fica gravada em `event` e logada, para quando o n8n voltar alguem responder "quando comecou?".

**Transicao, nao ciclo:** 30 s por ciclo numa queda de 1 h sao 120 registros identicos. O evento marca o inicio; a recuperacao so gera log (`n8n_ficou_de_pe`).

**Sem despachante:** `EventPipeline.enqueue` pula o que tem `notificar` False. Nao ha o que despachar -- e por isso que nao existe `ResolutionChecker._source_down`, e a ausencia e correta.

---

## D31 - Coletor de credenciais: `shared` nunca e guardado

**Decisao:** novo `sources/n8n/credentials.py`, ligado em `Agent.check_credentials()` no ciclo de 60 s. Estado anterior na tabela `resource` com `kind='credential'` -- que a migration 001 **ja previa** e nunca foi usada. Nenhuma migration nova.

**Formato verificado antes de codar** (nao suposto): `GET /api/v1/credentials` na instancia real responde HTTP 200 com 39 itens e os campos `id`, `name`, `type`, `createdAt`, `updatedAt`, `shared`.

**`shared` e descartado antes de gravar.** Traz `Endereco nio@grupocampanha.com.br <nio@...>` com papel de acesso -- e-mail de pessoa. Nao ha o que redactar se o dado nunca e guardado: o alerta leva a **quantidade** de quem tem acesso, nunca **quem**. Trado por tres testes (no banco, no alerta, e o `meta` gravado).

**Nao existe campo de autoria.** A API nao diz QUEM alterou, so QUEM tem acesso. O alerta fala do que mudou e quando, e o texto diz explicitamente que nao sabe quem -- um "Quem alterou" vazio seria pior que a linha ausente.

**Primeira coleta so forma base:** 39 credenciais no primeiro ciclo seriam 39 alertas. Mesma regra do `WorkflowMonitor`.

**403/500 nao viram linha de base:** tratar erro de permissao como "zero credenciais" faria a coleta seguinte tratar 39 credenciais como NOVAS -- 39 alertas por um 403.

**Fora de escopo, por decisao:** remocao de credencial. `resource.missing_since` existe no schema e **nada** o escreve -- nao ha API. Detectar remocao exigiria um `event_type` novo (regra, template, policy). Pendencia registrada, nao improvisada.

---

## D32 - Codigo morto removido, e o motivo de cada um

**Decisao:** removidos `N8NClient.version()`, `N8NClient.list_credentials()`, `resource_control_users()` e `stack_mudou()`, com os testes correspondentes.

**Verificado antes de remover** (um grep anterior com glob nao-recursivo ja tinha dado resultado errado):

- `resource_control_users` -- a resolucao de dono usa os **LABELS** do container (`donos.dono_da_stack`), nao `ResourceControl`.
- `stack_mudou` -- `stacks.StackCollector._processar` faz a comparacao **inline** com os proprios campos.
- `list_credentials` -- superseded por `iter_credentials`. Pior: a versao removida era de **pagina unica** e perdia o `nextCursor`, que e exatamente a classe de bug que D26 e o lote 2A corrigiram. Deixa-la disponivel convida o uso errado.
- `version()` -- zero referencias. O proprio codigo documenta que `/healthz` nesta instancia devolve so `{"status":"ok"}` e nao tem versao.

**Mantido: `workflow_history()`.** Sem chamador em `src/`, mas e a API da capacidade `versioning` que o `capability probe` ja detecta e registra. Remover deixaria a capacidade descoberta e inalcancavel -- codigo morto que e CAPACIDADE, nao residuo.

**Nao resolvido, e conhecido:** o cluster de resolucao de dono pelo Portainer (`resolver.resolve_for_portainer_resource` e `resolver.union`, tambem sem chamador) permanece. A diferenca: a docstring do modulo afirma que a definicao do dono e "n8n OU Portainer", o que hoje e verdade so pela metade. Corrigir a docstring e apagar as funcoes e uma mudanca de contrato do modulo -- decisao propria, fora desta passagem de higiene.

**A regra que guia:** complexidade sem falha nomeada nao fica. `reconciliation_gap`, `save_capabilities`, `Escalator` e `source_down`(o ADR D7 original) tinham exatamente este formato -- teste passa, nada produz. Codigo morto que e **testado** e o pior tipo, porque parece caredado.

---

## D33 - Alerta sai da fila em ate 50 s

**Decisao:** a fila gira a cada 10 s (`queue_seconds`, faixa 5-25 s) e a espera de reconferencia tem teto: `min(ciclo da fonte + 5 s, queue_max_delay_seconds - queue_seconds)`. Com 50 s e 10 s, o teto e 40 s.
**Antes:** espera de 1 ciclo + 5 s (ate 65 s) mais o tick da fila de 30 s -- ate ~95 s entre o evento entrar na fila e sair.
**Por que a fila pode girar abaixo de 25 s:** o piso de 25 s protege as FONTES. O tick da fila so le estado local (ultimo ciclo de cada coletor) e faz 1 consulta ao n8n por execucao pendente.
**Trade-off aceito:** fontes de 60 s (consumo, ativacao/desativacao de workflow) sao reconferidas aos 40 s, antes do proximo ciclo delas -- o descarte de problema que "passou sozinho" deixa de valer para elas. Erros e containers (30 s) seguem com 1 ciclo inteiro.
**Fora dos 50 s:** a redacao do LLM (~4 s medido, timeout 60 s) e o reenvio em falha (30/60/120/240 s).
**Travado por:** `test_alerta_persistente_sai_em_ate_50s` (ticks reais em qualquer fase) e `test_espera_mais_um_tick_nunca_passa_de_50s`.
**Reverte:** `queue_seconds`/`queue_max_delay_seconds` em `environments.yaml`.

---

## D34 - Dono pelo Portainer: as funcoes sem chamador sairam

**Decisao:** `RecipientResolver.resolve_for_portainer_resource`, `union` e `invalidate` foram removidas, e a docstring do modulo passou a dizer o que o codigo faz.
**Por que agora:** o D32 deixou isso em aberto como "decisao propria". O dono de stack/container ja chega por outro caminho -- `sources/portainer/donos.py` resolve `responsavel_stack`, e `resolve_pessoas` o transforma em e-mail. As tres funcoes eram codigo morto testado, o tipo que o D32 aponta como o pior.
**Reverte:** `git revert` do commit; nao ha chamador a religar.

---

## D35 - Power Automate entra pelo Dataverse, com o `nio-fabric-sp` so-leitura

**Decisao:** a fonte Power Automate le o Dataverse do ambiente (`flowruns`, `flowevents`, `workflows` com `category eq 5`) por client credentials do service principal `nio-fabric-sp`, com um secret SO do monitor e um usuario de aplicativo com a funcao `NIO Monitor Leitura` (Read em nivel Organization em Flow Run, Flow Event, Process, User e Organization -- esta ultima so para ler a retencao configurada; nada de escrita).
**Descartado:** Application Insights como fonte de alerta (so em ambiente gerenciado, entrega em ate 24 h); Purview (so ciclo de vida, nunca execucao); API `api.flow.microsoft.com` (nao documentada -- o mesmo risco que o D2 registra); System Administrator para o SP (a criacao por API sempre o concede -- por isso o cadastro e pela interface).
**Por que o secret separado:** rotacao e revogacao independentes do Fabric; um vazamento aqui nao obriga a girar o secret do NIO-CLI.
**Nao muda o Entra:** o acesso ao Dataverse vem do usuario de aplicativo, nao de API permission -- as restricoes do runbook do Fabric ("so Application", "sem admin-consent de Power BI") ficam intactas.
**Limitacao aceita:** so fluxos dentro de solucao gravam execucao no Dataverse.
**Primeiro passo, antes do coletor:** `doctor` sonda acesso, retencao, fluxos em solucao, valores reais de `status`, atraso de ingestao (`createdon - endtime`) e avisos de perda. Os 50 s do D33 so valem para esta fonte se o atraso medido couber.

---

## D36 - RPA e `flowsession`, nao `flowrun`; tipo de evento proprio

**Medido em 14/10/2026 no ambiente RPA-CONTAS-A-RECEBER (`org7dd46aa7`):** 32 fluxos de DESKTOP ligados e so 3 fluxos de nuvem (de sistema do Dynamics, desligados). `flowrun` vazia. Execucao de fluxo de desktop vai para `flowsession`, sempre, dentro ou fora de solucao. Status reais: 8 = Failed, 4 = Succeeded. Atraso de ingestao: 0 s (a sessao e gravada pelo proprio servico).
**Decisao:** coletor `DesktopFlowCollector` (`sources/powerautomate/desktop.py`) a cada `execution_errors_seconds`; Failed/Faulted/TimedOut somam no streak do fluxo (chave `pa:<id>`, para nao colidir com id de workflow do n8n), Succeeded encerra o incidente. Evento `desktop_flow_failed`, com regras proprias na policy (3 seguidas = critico, 1 = alto) e texto de acao do Power Automate.
**Por que tipo proprio, e nao `execution_failed`:** o texto de acao ("abrir no n8n"), a reconferencia (API do n8n) e o botao do e-mail eram do n8n. Reaproveitar o tipo levaria o operador para o lugar errado.
**Cursor:** `completedon` tem precisao de segundo; a consulta usa `ge` e o cursor guarda os ids do ultimo segundo. Com `gt`, uma sessao que termina no mesmo segundo da ultima lida, mas entra depois da consulta, some em silencio.
**Primeira coleta:** olha as ultimas 24 h -- um RPA quebrado ha dias alerta no primeiro ciclo, com o streak da janela (nao o historico inteiro).
**Destinatario (decisao do dono, 14/10/2026):** SO a lista `fontes: powerautomate` do `people.yaml` (rafael.barboza e carlos.henrique). O dono da sessao (`owninguser`) e a conta tecnica `NaturalFarms@rpanf.onmicrosoft.com`, que ninguem le -- segue visivel no e-mail como "Responsavel pelo fluxo", mas nao recebe. Travado por `test_alerta_de_rpa_sai_no_padrao_para_os_destinatarios_da_fonte`.
**Privilegios adicionais na funcao:** Read (Organization) em `flowsession`, `flowmachine`, `flowmachinegroup` -- concedidos por `AddPrivilegesRole` com login do admin, porque `Flow Run` nao aparece no editor de funcoes deste ambiente.
**Sem botao no e-mail por enquanto:** o link do Power Automate exige o ID do ambiente, que nao esta na config.
