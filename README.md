# AGENT NIO MONITOR

Agente que observa **n8n** e **Portainer CE**, detecta falha e mudança, e notifica
o responsável certo.

## O que ele resolve

| Antes | Depois |
|---|---|
| Evento passa despercebido | Monitoramento contínuo |
| Erro só aparece quando explode | Detecção na primeira execução com erro |
| Ninguém sabe quem é o responsável | Destinatário resolvido por n8n project / Portainer ResourceControl |
| Impacto de mudança não avaliado | Diff estrutural + análise de risco determinística |
| Comunicação manual | Email automático, **detalhado porém direto** |

## Status atual

**1330 testes passando** (+3 do LLM, pulados com `SKIP_LLM_TESTS=1`), lint limpo, cobertura de **93%**.
Suíte completa em ~18 s (sem cobertura). Nenhum arquivo de `src/` passa de 300 linhas.

| Ticket | Entrega | Estado |
|---|---|---|
| T1 | Feature-detect, config, schema Postgres, migrations versionadas | OK |
| T2 | Coleta por cursor + detector de buraco + redator | OK |
| T3 | Streak + motor de regras + destinatarios | OK |
| T4 | LLM redige + templates de email | OK |
| T5 | Diff estrutural de workflow | OK — job `workflows_full` |
| T6 | Cliente Portainer | OK — jobs `containers_*` e `stacks` |
| T7 | HealthServer + dead-man's switch | HealthServer OK; FALTA instalar o cron externo |

### Bugs achados e corrigidos na auditoria

A auditoria de refino encontrou defeitos em codigo que eu havia declarado pronto:

| Severidade | Defeito | Como foi pego |
|---|---|---|
| Critico | `record_executions` usava `COPY` com `columns=` (inexistente no psycopg3), nao alimentava as linhas e nao tinha `ON CONFLICT` | Inspecao da assinatura real de `Cursor.copy` |
| Critico | `bump_streak` da InMemory zerava o contador; a regra `min_streak: 3` **nunca** chegava a `critical` | Teste com horario CRESCENTE (o caso real de um workflow que comeca a falhar) |
| Critico | `Streak` sem `tenant_id`/`workflow_name`; o Postgres devolvia objeto sem eles | Leitura do dataclass contra o `RETURNING` |
| Alto | 503 do gateway nao era retentado | Teste de politica de retry |
| Alto | `duration_ms` negativo (-3600000 ms) com clock skew | Teste de borda |
| Alto | `add_signal_handler` derrubava o boot no Windows | Execucao direta: `NotImplementedError` |
| Alto | 18 arquivos com BOM, que quebra `ast.parse` e `pglast` | `ast.parse` falhando no script de limpeza |
| Medio | `senha` (portugues) nao era redigida | Teste de borda com chave pt-BR |
| Medio | `formatar_horas` mostrava UTC sem rotulo num servidor em UTC-3 — 6 h de erro | Teste de borda de fuso |
| Medio | Bootstrap SQL dentro de `migrations/` — o agente tentaria `CREATE ROLE` no boot | Teste de fronteira |

### O anti-padrão que Enabling e que estes testes fecham

`InMemoryRepository` e `PostgresRepository` divergiam, e os testes rodavam so
contra a InMemory. Um mock que mente e pior que nenhum mock: o teste passa,
a producao falha. `tests/integration/test_paridade_postgres.py` agora executa as
DUAS implementacoes com a MESMA entrada e compara resultado. Enquanto ele nao
rodar, mock e producao podem divergir de novo.

---

## Como rodar

```powershell
uv venv --python 3.12
uv pip install -e ".[dev]"
Copy-Item .env.example .env      # preencha
```

### 1. Descobrir o que a instância expõe

```powershell
.\.venv\Scripts\python.exe -m agent_nio_monitor.main doctor
```

Sonda endpoints e imprime a **versão real do n8n** (vem de `/healthz`, não de
suposição). Essa é a resposta para a pergunta que ficou aberta no planejamento.

### 2. Uma passada de coleta

```powershell
.\.venv\Scripts\python.exe -m agent_nio_monitor.main once
```

### 3. Loop contínuo

```powershell
.\.venv\Scripts\python.exe -m agent_nio_monitor.main run
# health em http://localhost:8099/healthz
```

### Testes

```powershell
.\.venv\Scripts\python.exe -m pytest -q                      # tudo
$env:SKIP_LLM_TESTS="1"; .\.\.venv\Scripts\python.exe -m pytest -q   # sem LLM
.\.venv\Scripts\python.exe -m ruff check src tests --select F,E9,B,SIM,UP
```

---

## Coleta profunda (ADR D39–D41)

| Fonte | O que é lido | Alerta novo |
|---|---|---|
| n8n | Cada execução **com sucesso**, nó a nó (`runData` + `workflowData`) | `node_error_hidden` (erro em execução verde), `node_not_executed` (etapa do caminho normal que não rodou, com a causa no grafo). Falha de execução ganha o caminho executado |
| Portainer | Log incremental de cada container, por assinatura de mensagem | `container_log_error` (mensagem nova ou pico), `workflow_trigger_failed` (gatilho do n8n falhando, lido do log do container) |
| Power Automate | Fase do runtime e log de cada ação da sessão | Falha diz a fase, o significado do código e a ação; `desktop_flow_handled_errors` (sessão verde com erro tratado) |

Aprendizado e catálogo ficam na tabela `resource` (sem migration nova). Ruído de log se ajusta
em `policy.yaml: logs.ignorar`.

## Investigação, sem duplicidade e avisos verdes (ADR D42–D44)

- **Qwen investiga antes de enviar:** com o tool calling Hermes do vLLM, lê log, inspect do
  container, execução do n8n e ações do RPA, e só então escreve a causa. Cada evidência citada é
  conferida literalmente no que ele leu; o e-mail mostra "Causa apurada (confiança X)".
- **Um alerta por incidente:** o mesmo problema não reenvia enquanto durar; edição de
  workflow/credencial/stack vira um e-mail só, 10 min depois do último save.
- **Verdes:** criação de workflow, container e fluxo de RPA, e refactor de workflow (score de
  risco ≤ `refactor_max_score`).

## Notificações: fila, e-mail HTML e tempos

- **Fila interna de disparo** (`notification_queue`, migration 004). Nada sai na hora: o evento
  entra na fila e espera 1 ciclo da fonte. Na reconferência (`notify/resolution.py`), se o
  problema passou (container voltou, execução posterior com sucesso, consumo normalizou...) o
  alerta é **descartado**; se continua, sai **uma vez só** (um pendente por problema, e a mesma
  chave não repete dentro da janela de re-notificação da regra). Falha de envio reagenda
  (30/60/120/240 s) e desiste após 5 tentativas. Fatos (workflow/stack alterados) não têm o que
  resolver e saem no primeiro ciclo. `/healthz` mostra `queue.pending`.
- **Tempos:** coleta entre 25 e 60 s (erros e containers 30 s; workflows, stacks e consumo
  60 s). A fila gira a cada **10 s** (5–25 s) e o alerta sai em **até 50 s** depois de entrar
  nela (`queue_max_delay_seconds`): a espera de reconferência é 1 ciclo da fonte + 5 s, com
  teto de 50 − 10 = 40 s. Fontes de 60 s (consumo, ativação de workflow) são reconferidas antes
  do próximo ciclo delas — o preço do alerta em 50 s. O tempo de redação do LLM (~4 s medido)
  vem depois disso. Valores fora das faixas são recusados no boot (`PollingConfig`).
- **E-mail HTML com a arte JFC:** `notify/html/` (vermelho = CRÍTICO/ALTO, amarelo = MÉDIO,
  verde = BAIXO/INFO). As imagens ficam em `assets/email/`, geradas por
  `scripts/recortar_arte.py` a partir do tríptico em `assets/`, e servidas pelo GitHub
  (`mail.assets_base_url`). Horários em Brasília. O texto simples segue como alternativa.
- O workflow do n8n (`n8n/workflow_envio_email.json`) envia `emailFormat: both` (HTML + texto).

## Verificações que sustentam o desenho

Estes testes usam **dados reais** da sua instância, não inventados:

- **Redação**: os dois segredos que existem no seu `GET /workflows`
  (`Password: jfc2025` do SAP, `user=postgres password=postgres` do `dblink`).
- **Política**: as 25 execuções com erro mais recentes, com os horários reais.
- **Coleta**: o cursor invalidados pelo prune, e o caso de 400 em vez de 404.
- **LLM**: Qwen (`RedHatAI/Qwen3.8-27B-INT4`, vLLM) real em `192.168.0.140:8001/v1`.

---

## O que Não está verificado

**Verificado agora:** com `DATABASE_DSN_TEST` no `.env`, as suítes de `tests/integration/` rodam contra o
Postgres de teste (`hermes_test`) — inclusive a paridade InMemory × Postgres do streak
(`test_paridade_postgres.py`) e das escritas de catálogo (`test_paridade_catalogo.py`).
`postgres_queue.py` e `postgres_records.py` estão em 100%.

**Ainda não** verificado: comportamento em volume (partições cheias, `drop_partitions_older_than`
com meses de dado).

---

## Riscos abertos

| Risco | Situação |
|---|---|
| **Silêncio** | Se o n8n cai, o alerta vai para log (decisão do dono). **Precisa de consumidor** — ver `docs/runbook.md` |
| **Atribuição** | `activeVersion.authors` existe na sua instância e é lido. Se o `doctor` mostrar `versioning: nao`, a autoria volta a ser indisponível |
| **Bandwidth** | Corpo completo dos workflows a cada 60 s (11,2 MB por leitura) ≈ **16 GB/dia** em LAN — custo aceito pelo dono para a edição chegar em até 1 min (ver ADR D6) |
| **Fadiga** | Política entrega ~11 emails para 25 erros em 25h (piso de 8: um por workflow quebrado). Monitorar taxa de falso positivo |

Ver `docs/runbook.md` para operação e `docs/adr/` para o porquê de cada decisão.