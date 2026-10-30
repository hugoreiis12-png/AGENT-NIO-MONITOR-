"""Acao sugerida por template: a ultima secao de todo alerta."""

from __future__ import annotations

# Acao padrao por template. Sem isso o render quebrava com KeyError -- o que
# significa que NENHUM email saía. Um template que estoura em producao e o mesmo
# que template inexistente.
ACAO_PADRAO = {
    "workflow_failing": (
        "Abrir o workflow no n8n e olhar o ultimo no executado. "
        "Se a falha se repete no mesmo horario, e job agendado quebrado, "
        "nao incidente."
    ),
    "execution_failed": "Abrir a execucao no n8n e ver o erro completo.",
    "desktop_flow_failing": (
        "Abrir o Power Automate em Monitorar > Execucoes de fluxo da area de trabalho e ver o erro. "
        "Erro de logon (WindowsIdentityIncorrect) e conexao da maquina com usuario ou senha errados."
    ),
    "desktop_flow_failed": "Abrir o Power Automate em Monitorar > Execucoes de fluxo da area de trabalho e ver o erro.",
    "workflow_changed": "Conferir se a mudanca era esperada e o que mais mudou junto.",
    "workflow_deactivated": "Confirmar com quem desativou e reativar se foi engano.",
    "workflow_activated": "Nada a fazer. Registro para auditoria.",
    "credential_updated": "Confirmar a rotacao e verificar se algum workflow depende do valor antigo.",
    "container_down": "Verificar os logs do container e o resource limit.",
    "restart_loop": "Checar memoria/CPU e a ultima alteracao no servico.",
    "reconciliation_gap": "O monitor perdeu eventos. Conferir se o n8n podou execucoes antigas.",
    "source_down": "Restaurar o servico. O monitor nao consegue notificar por email enquanto isso.",
    "workflow_created": "Confirmar com o autor que o workflow era esperado e revisar credenciais e gatilhos.",
    "stack_changed": "Conferir com quem alterou se a mudanca era esperada e se os servicos subiram.",
    "container_removed": "Confirmar se a remocao foi intencional; se nao, recriar a partir da stack.",
    "container_resource_high": "Ver o que consome o recurso e ajustar o limite ou escalar o servico.",
    "node_error_hidden": (
        "Abrir a execucao no n8n e ver o no em erro. Ele esta com 'continuar em erro', "
        "entao a falha nao aparece como execucao com erro."
    ),
    "node_not_executed": (
        "Abrir a execucao no n8n e conferir o no indicado como causa: um IF que mudou de ramo "
        "ou um no que passou a devolver 0 itens costuma ser a origem."
    ),
    "workflow_trigger_failed": (
        "O workflow nao esta disparando. Conferir a credencial e a conexao do gatilho no n8n "
        "(ex.: IMAP, webhook) e reativar o workflow se preciso."
    ),
    "container_log_error": "Ver o log completo do container no Portainer e o que mudou antes do erro aparecer.",
    "dependency_silent": (
        "A automacao esperou entrega da ponte do n8n e nao recebeu. Conferir as execucoes do workflow "
        "mapeado na secao Correlacoes: se nao rodou, reativar/investigar o workflow; se rodou verde e "
        "respondeu vazio, o problema esta no deposito anterior (IMAP/portal) ou no consumo."
    ),
    "desktop_flow_handled_errors": (
        "Abrir a execucao no Power Automate e ver a acao indicada: o fluxo seguiu em frente, "
        "mas o resultado pode estar incompleto."
    ),
    "workflow_refactored": "Nada a fazer: a alteracao nao muda o resultado do fluxo. Registro para auditoria.",
    "container_created": "Confirmar que o container era esperado e que a stack subiu como planejado.",
    "desktop_flow_created": "Confirmar com o responsavel que o fluxo era esperado e se ja tem maquina e agenda.",
    "bubble_api_unreachable": (
        "1. Verificar o status da API do Bubble no painel de monitoramento do app. "
        "2. Conferir a conectividade de rede do host Docker com o endpoint da API "
        "(DNS, firewall, rota). "
        "3. Se a falha for de timeout, verificar se o servico do Bubble esta sobrecarregado. "
        "4. Se a falha persistir, abrir chamado no suporte do Bubble com o endpoint e o status HTTP."
    ),
    "generic": "Ver o detalhe do evento.",
}
