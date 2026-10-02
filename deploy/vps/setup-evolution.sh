#!/usr/bin/env bash
# Cria a instância do WhatsApp na SUA Evolution API (self-hosted) e aponta o webhook para o transformsite.
# Rodar na VPS, dentro de deploy/vps, depois de `docker compose --profile whatsapp up -d`.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
INSTANCE=${1:-transformsite}
API="https://$EVOLUTION_DOMAIN"

echo "1) criando instância $INSTANCE…"
curl -fsS -X POST "$API/instance/create" -H "apikey: $EVOLUTION_API_KEY" -H 'Content-Type: application/json' \
  -d "{\"instanceName\":\"$INSTANCE\",\"integration\":\"WHATSAPP-BAILEYS\",\"qrcode\":true}" >/dev/null || echo "   (já existe?)"

echo "2) configurando webhook → http://app:8000/webhook/whatsapp"
curl -fsS -X POST "$API/webhook/set/$INSTANCE" -H "apikey: $EVOLUTION_API_KEY" -H 'Content-Type: application/json' \
  -d '{"webhook":{"enabled":true,"url":"http://app:8000/webhook/whatsapp","byEvents":false,"base64":false,"events":["MESSAGES_UPSERT"]}}' >/dev/null

echo "3) abra no navegador para ler o QR Code com o WhatsApp do número de atendimento:"
echo "   $API/manager  (API key: a EVOLUTION_API_KEY do .env)"
echo "4) no projeto, confira channels.whatsapp.instance: $INSTANCE e base_url: http://evolution:8080"
