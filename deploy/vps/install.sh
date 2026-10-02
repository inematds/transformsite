#!/usr/bin/env bash
# Instalação numa VPS Ubuntu/Debian limpa (rodar como root ou com sudo).
#   curl -fsSL https://raw.githubusercontent.com/inematds/transformsite/main/deploy/vps/install.sh | bash -s -- \
#        --domain bot.suaempresa.com.br --site https://www.suaempresa.com.br --org "Sua Empresa" --contact atendimento@suaempresa.com.br
set -euo pipefail

DOMAIN=""; SITE=""; ORG="Minha Empresa"; CONTACT="contato@exemplo.com"; DIR=/opt/transformsite; WHATS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN=$2; shift 2;;
    --site) SITE=$2; shift 2;;
    --org) ORG=$2; shift 2;;
    --contact) CONTACT=$2; shift 2;;
    --dir) DIR=$2; shift 2;;
    --whatsapp) WHATS=1; shift;;
    *) echo "opção desconhecida: $1"; exit 1;;
  esac
done
[ -n "$DOMAIN" ] && [ -n "$SITE" ] || { echo "uso: install.sh --domain <dominio> --site <url do site> [--org ..] [--contact ..] [--whatsapp]"; exit 1; }

command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
apt-get update -qq && apt-get install -y -qq git python3-venv >/dev/null

[ -d "$DIR" ] || git clone --depth 1 https://github.com/inematds/transformsite.git "$DIR"
cd "$DIR/deploy/vps"

# projeto do bot (configuração + serviços) usando o próprio CLI dentro de um venv
python3 -m venv /tmp/ts-venv && /tmp/ts-venv/bin/pip install -q "$DIR"
[ -d projeto ] || /tmp/ts-venv/bin/transformsite init projeto --org "$ORG" --url "$SITE" --contact "$CONTACT"

if [ ! -f .env ]; then
  cp .env.example .env
  rnd() { head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40; }
  sed -i "s|^DOMAIN=.*|DOMAIN=$DOMAIN|; s|^EVOLUTION_DOMAIN=.*|EVOLUTION_DOMAIN=whats.$DOMAIN|" .env
  sed -i "s|^ADMIN_TOKEN=.*|ADMIN_TOKEN=$(rnd)|; s|^EVOLUTION_API_KEY=.*|EVOLUTION_API_KEY=$(rnd)|; s|^EVOLUTION_DB_PASSWORD=.*|EVOLUTION_DB_PASSWORD=$(rnd)|" .env
fi
# dentro do compose, Ollama e Evolution são acessados pelo nome do serviço
sed -i 's|base_url: http://localhost:11434|base_url: http://ollama:11434|; s|base_url: "http://localhost:8080"|base_url: "http://evolution:8080"|' projeto/transformsite.yaml
chown -R 1000:1000 projeto

PROFILE=""; [ "$WHATS" = 1 ] && PROFILE="--profile whatsapp"
docker compose $PROFILE up -d --build
docker compose run --rm ollama-pull
docker compose exec app transformsite ingest

echo
echo "Pronto: https://$DOMAIN/chat (widget) · https://$DOMAIN/admin (painel; token em $DIR/deploy/vps/.env)"
[ "$WHATS" = 1 ] && echo "WhatsApp: rode ./setup-evolution.sh para criar a instância e ler o QR Code."
