#!/usr/bin/env bash
# Dead-man's switch do agent-nio-monitor (ADR D46). Rode FORA do host do Docker do agente.
set -u

: "${HERMES_HEALTH_URL:?}" "${HERMES_ALERTA_PARA:?}" "${SMTP_URL:?}" "${SMTP_FROM:?}" "${SMTP_USERNAME:?}" "${SMTP_PASSWORD:?}"
ESTADO="${HERMES_ESTADO:-/var/tmp/hermes-deadman.estado}"
CORPO="$(mktemp)"; MSG="$(mktemp)"
trap 'rm -f "$CORPO" "$MSG"' EXIT

ler_estado() {
  local codigo
  codigo="$(curl -sS --max-time 10 -o "$CORPO" -w '%{http_code}' "$HERMES_HEALTH_URL" 2>/dev/null)" || codigo="000"
  case "$codigo" in
    200) echo "OK $codigo" ;;
    503) echo "DEGRADADO $codigo" ;;
    *)   echo "MORTO $codigo" ;;
  esac
}

enviar() {
  local estado="$1" codigo="$2"
  printf 'From: %s\r\nTo: %s\r\nSubject: [HERMES] agente %s (http %s)\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n%s\r\n\r\n%s\r\n' \
    "$SMTP_FROM" "$HERMES_ALERTA_PARA" "$estado" "$codigo" "Lido de $HERMES_HEALTH_URL em $(date -Is)" "$(head -c 3000 "$CORPO")" > "$MSG"
  # Senha por stdin (--config -): nao aparece na lista de processos.
  printf 'user = "%s:%s"\n' "$SMTP_USERNAME" "$SMTP_PASSWORD" \
    | curl -sS --max-time 30 --ssl-reqd --url "$SMTP_URL" --mail-from "$SMTP_FROM" --mail-rcpt "$HERMES_ALERTA_PARA" \
      --upload-file "$MSG" --config - \
    || logger -t HERMES "HERMES-DEADMAN: falha ao enviar e-mail de $estado"
}

read -r atual codigo <<< "$(ler_estado)"
{ read -r confirmado visto repeticoes < "$ESTADO"; } 2>/dev/null || { confirmado="OK"; visto="OK"; repeticoes=0; }
if [ "$atual" = "$visto" ]; then repeticoes=$((repeticoes + 1)); else repeticoes=1; fi

# Avisa so na transicao confirmada por 2 leituras seguidas (10 min no cron de 5 min).
if [ "$atual" != "$confirmado" ] && [ "$repeticoes" -ge 2 ]; then
  logger -t HERMES "HERMES-$atual: http=$codigo (antes $confirmado)"
  enviar "$atual" "$codigo"
  confirmado="$atual"
fi
echo "$confirmado $atual $repeticoes" > "$ESTADO"
