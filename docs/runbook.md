# Runbook — operação

## 1. Antes de subir

```powershell
Copy-Item .env.example .env   # preencha N8N_API_KEY, PORTAINER_API_KEY, DATABASE_DSN
$env:DATABASE_DSN="postgresql://usuario:senha@host:5432/hermes"

# 1) Descobrir o que a instância expõe (e a versão real do n8n)
.\.venv\Scripts\python.exe -m agent_nio_monitor.main doctor
```

O `doctor` é o passo que substitui suposição. Ele imprime a versão vinda de
`/healthz` e o resultado de cada sonda.

### Credenciais

- **n8n:** a key precisa ser **owner/admin** para `/users` e `/projects/{id}/users`.
  Sem isso o agente cai no fallback `config/people.yaml` e registra isso no log.
- **Portainer:** precisa ser **admin** para `/api/users` (sem isso, resolução de
  responsável por Portainer não funciona).

---

## 2. Dead-man's switch (OBRIGATÓRIO)

O Hermes alerta por log quando o n8n cai. Se **ninguém consome esse log**, o
resultado é um monitor que falha em silêncio — o pior modo de falha possível.

Instale em uma máquina **diferente** da que roda o Hermes:

```bash
# crontab, a cada 5 minutos
*/5 * * * * curl -fsS --max-time 10 http://IP_DO_HERMES:8099/healthz -o /dev/null \
  || logger -t HERMES "HERMES-MORTO: sem heartbeat ha mais de 5min"
```

E um shipper consumindo o alerta em stderr:

```bash
# journald: journalctl -f -t hermes  (ou equivalente)
# Loki/Elastic: filtrar por prefixo "HERMES-ALERTA"
```

> O `healthz` responde **503** quando alguma fonte está stale (não coletando há
> mais de 10 min) ou quando o n8n está fora. Isso distingue "processo vivo" de
> "processo vivo mas não coletando" — são falhas diferentes.

---

## 3. Retenção

Partições são criadas para 2 meses à frente no boot. Para reter mais:

```sql
SELECT hermes.ensure_partitions(4);          -- 4 meses à frente
SELECT * FROM hermes.drop_partitions_older_than(90);
```

Agende `drop_partitions_older_than` mensalmente. `DROP PARTITION` é instantâneo;
`DELETE` em massa sobre tabela particionada não libera espaço e trava o vacuum.

---

## 4. Diagnóstico

| Sintoma | Onde olhar |
|---|---|
| Nenhum email chega | `SELECT status, recipients, error FROM hermes.notification ORDER BY ts DESC LIMIT 20;` |
| Destinatário errado | log `destinatario_por_fallback` — mostra `fonte` e `motivo` |
| Eventos sumindo | `SELECT * FROM hermes.reconciliation_run ORDER BY run_at DESC LIMIT 20;` — `gaps > 0` ou `cursor_valid = false` |
| Erro de coleta | `SELECT * FROM hermes.source_cursor;` — `consecutive_failures` |
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
    escalate_after_minutes: [15, 45]
```

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