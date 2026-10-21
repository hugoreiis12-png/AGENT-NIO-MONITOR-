# Runbook — operação

## 1. Antes de subir

```powershell
Copy-Item .env.example .env   # preencha N8N_API_KEY, PORTAINER_API_KEY, DATABASE_DSN
$env:DATABASE_DSN="postgresql://usuario:senha@host:5432/hermes"

# 1) Descobrir o que a instância expõe (e a versão real do n8n)
.\.venv\Scripts\python.exe -m agent_nio_monitor.main doctor
```

O `doctor` é o passo que substitui suposição. Ele imprime o resultado de cada
sonda (esta instância do n8n não expõe versão: `/healthz` devolve só o status).

### Credenciais

- **n8n:** `/projects` responde 403 por **limite de licença**
  (`feat:projectRole:admin`), não por papel da key (ADR D29). Trocar a key não
  resolve: `config/people.yaml` (`projects:`) é o caminho normal de destinatário.
- **Portainer:** o dono de stack/container vem dos **labels** do container
  (ADR D34); `/api/users` só aparece como capacidade no `doctor`.
- **Power Automate:** `POWERAUTOMATE_*` (service principal `nio-fabric-sp`, ADR D35/D36).
  Sem elas só o ciclo de RPA fica desligado.

---

## 2. Dead-man's switch (OBRIGATÓRIO)

O Hermes alerta por log quando o n8n cai. Se **ninguém consome esse log**, o
resultado é um monitor que falha em silêncio — o pior modo de falha possível.

Instale `scripts/deadman.sh` (ADR D46) em uma máquina Linux **diferente** da que roda o Hermes.
Ele lê o `/healthz` (200 = OK, 503 = DEGRADADO, resto = MORTO) e manda e-mail por SMTP direto
só na **transição**, confirmada por 2 leituras seguidas (10 min):

```bash
sudo install -D -m 750 scripts/deadman.sh /opt/hermes/deadman.sh
sudo install -m 600 /dev/null /etc/hermes-deadman.env   # preencha:
# HERMES_HEALTH_URL=http://IP_DO_HERMES:8099/healthz
# HERMES_ALERTA_PARA=destinatario@dominio
# SMTP_URL=smtps://pop.grupocampanha.com.br:465
# SMTP_FROM=nio@grupocampanha.com.br
# SMTP_USERNAME=...
# SMTP_PASSWORD=...
crontab -e
# */5 * * * * set -a; . /etc/hermes-deadman.env; set +a; /opt/hermes/deadman.sh
```

Teste: aponte `HERMES_HEALTH_URL` para uma porta fechada e rode duas vezes. A 2ª execução
envia "[HERMES] agente MORTO (http 000)". O estado fica em `/var/tmp/hermes-deadman.estado`.

E um consumidor do alerta em stderr. No deploy atual (stack do Portainer) o
stderr vai para o log do container `agent-nio-monitor`:

```bash
# no host do Docker (ou pelo Portainer: Containers > agent-nio-monitor > Logs)
docker logs -f agent-nio-monitor 2>&1 | grep --line-buffered "HERMES-ALERTA"
# Loki/Elastic: coletar o log do container e filtrar pelo prefixo "HERMES-ALERTA"
```

O stderr só recebe alerta com o **n8n fora** (ADR D8). Webhook falhando com o n8n
de pé não vai para o log: a fila reagenda e, após 5 tentativas, o item fica
`failed` em `notification_queue` (ADR D37).

> O `healthz` responde **503** quando alguma fonte está stale (não coletando há
> mais de 10 min) ou quando o n8n está fora. Isso distingue "processo vivo" de
> "processo vivo mas não coletando" — são falhas diferentes.

---

## 3. Retenção

O agente mantém as partições sozinho (ADR D38): no boot e a cada 6 h garante o
mês anterior, o corrente e o seguinte. A retenção é opcional, em
`config/environments.yaml`:

```yaml
database:
  retention_days: 90   # null = guarda tudo; mínimo 60
```

Com o valor definido, o mesmo job roda `drop_partitions_older_than` (log
`particoes_removidas`). `DROP PARTITION` é instantâneo e **irreversível**;
`DELETE` em massa sobre tabela particionada não libera espaço e trava o vacuum.
Manualmente:

```sql
SELECT hermes.ensure_partitions(4);          -- 4 meses à frente
SELECT * FROM hermes.drop_partitions_older_than(90);
```

---

## 4. Diagnóstico

| Sintoma | Onde olhar |
|---|---|
| Nenhum email chega | `SELECT status, recipients, error FROM hermes.notification ORDER BY ts DESC LIMIT 20;` — `failed` com 404 no webhook = workflow de envio desativado no n8n |
| Alerta não saiu / saiu atrasado | `SELECT status, event_type, resolved_reason, last_error FROM hermes.notification_queue ORDER BY created_at DESC LIMIT 20;` — `discarded` com motivo = o problema passou antes do envio; `pending` parado = veja `/healthz` → `queue` |
| Destinatário errado | log `destinatario_por_fallback` — mostra `fonte` e `motivo` |
| Eventos sumindo | `SELECT * FROM hermes.reconciliation_run ORDER BY run_at DESC LIMIT 20;` — `gaps > 0` ou `cursor_valid = false` |
| Erro de coleta | `SELECT * FROM hermes.source_cursor;` — `consecutive_failures` |
| Alerta de "etapa nao rodou" falso | `SELECT name, jsonb_array_length(meta->'janela') FROM hermes.resource WHERE kind = 'node-baseline';` — o caminho normal so vale com 20+ execucoes; workflow alterado reaprende sozinho |
| Erro de log ruidoso | acrescente uma regex em `policy.yaml: logs.ignorar.<container>`; catalogo em `SELECT name, meta FROM hermes.resource WHERE kind = 'log-signature';` |
| Gatilho do n8n nao dispara | alerta `workflow_trigger_failed` (lido do log do container `n8n`); 3 falhas na hora para alertar |
| Causa errada no e-mail | log `investigacao_concluida` (chamadas, evidencias, descartadas, confianca) ou `investigacao_interrompida`; `llm.investigacao: false` desliga e volta a analise so dos fatos |
| Alerta que "deveria ter repetido" | um alerta por incidente (ADR D43): so reenvia quando o problema passa e volta. `notification_queue.dedup_key` mostra o incidente |
| LLM caindo | log `llm_indisponivel_usando_fallback` + `status = 'fallback'` na notificação |
| Bandeira | `SELECT max(started_at) FROM hermes.execution;` — se não avança, a coleta parou |

---

## 5. Calibrando a política

`config/policy.yaml` é a **única** fonte de verdade sobre o que é alerta. Não
há lógica de risco no código — foi deliberado (dois lugares decidindo o que é
grave é como uma config começa a mentir).

Ajustes comuns:

```yaml
# Se o mesmo workflow queima a fila toda manhã:
rules:
  - name: "workflow_falhando_repetidamente"
    event_types: ["execution_failed"]
    severity: critical
    min_streak: 3
    re_notify_after_minutes: 720     # maior = menos repetição
```

`escalate_after_minutes` aparece em algumas regras, mas é **inerte**: a escalação
não está ligada (ADR D24). Mudar o valor não muda nada.

Depois de ajustar, rode:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_policies.py -q
```

O teste roda as **25 falhas reais** da sua instância e falha se a contagem de
notificações sair da faixa esperada.

---

## 6. O que fazer quando o n8n cai

1. O Hermes detecta por `/healthz` e escreve `HERMES-ALERTA` em stderr.
2. Sem email — o canal morreu junto com o n8n. **Use o log.**
3. Ao voltar, o n8n pode ter podado execuções → cursor inválida → buraco
   registrado em `reconciliation_run` com `gaps = 1`. **Isso é esperado e
   sinalizado**, não é bug.
4. Confira se o buraco corresponde ao período em que o n8n esteve fora. Se sim,
   o histórico é irrecuperável para esse intervalo — e o Hermes vai dizer isso.

---

## 7. Cobertura de teste contra ambiente real

```powershell
# LLM desligado (sem rede)
$env:SKIP_LLM_TESTS="1"; pytest -q

# Postgres real (pula sozinho se não houver DSN)
$env:DATABASE_DSN="postgresql://..."; pytest tests/integration -q
```