# Deploy numa VPS

Uma VPS Linux (Ubuntu 22.04+/Debian 12) com **4 vCPU / 8 GB RAM** roda o bot com um modelo pequeno
(`qwen2.5:7b`). Com GPU NVIDIA, descomente o bloco `deploy:` do serviço `ollama` e use um modelo maior.

## O que sobe

| Serviço | Papel | Porta interna |
|---|---|---|
| `app` | transformsite (`serve`): widget, webhooks, painel `/admin`, pollers | 8000 |
| `ollama` (+ `ollama-pull`) | LLM e embeddings locais | 11434 |
| `evolution` + `evolution-db` + `evolution-redis` | WhatsApp via Evolution API v2 (perfil `whatsapp`) | 8080 |
| `caddy` | HTTPS automático (Let's Encrypt) para `DOMAIN` e `EVOLUTION_DOMAIN` | 80/443 |

## Instalação automática

1. Aponte o DNS (registro A) de `bot.suaempresa.com.br` e `whats.bot.suaempresa.com.br` para o IP da VPS.
2. Rode:

```bash
curl -fsSL https://raw.githubusercontent.com/inematds/transformsite/main/deploy/vps/install.sh | sudo bash -s -- \
  --domain bot.suaempresa.com.br --site https://www.suaempresa.com.br \
  --org "Sua Empresa" --contact atendimento@suaempresa.com.br --whatsapp
```

O script instala Docker, clona o repositório em `/opt/transformsite`, cria o projeto em `deploy/vps/projeto`,
gera senhas aleatórias no `.env`, sobe os containers, baixa os modelos e roda o primeiro `ingest`.

## Manual

```bash
git clone https://github.com/inematds/transformsite /opt/transformsite && cd /opt/transformsite/deploy/vps
cp .env.example .env && nano .env                 # domínio, tokens, modelo
pipx install /opt/transformsite && transformsite init projeto --org "..." --url https://... --contact ...
docker compose --profile whatsapp up -d --build   # sem WhatsApp: docker compose up -d --build
docker compose run --rm ollama-pull
docker compose exec app transformsite ingest
```

## WhatsApp (Evolution)

```bash
./setup-evolution.sh            # cria a instância e aponta o webhook para http://app:8000/webhook/whatsapp
```

Depois abra `https://whats.SEU_DOMINIO/manager`, entre com a `EVOLUTION_API_KEY` e leia o QR Code com o
WhatsApp do número de atendimento. **Atenção:** a Evolution usa o WhatsApp Web (não oficial) — há risco de
bloqueio do número em uso abusivo. Para operação crítica, use a Cloud API oficial da Meta
(`channels.whatsapp.provider: cloud`).

## Telegram

Crie o bot no @BotFather, coloque `TELEGRAM_BOT_TOKEN` no `.env` e reinicie: `docker compose restart app`.
O bot usa long polling (não precisa de webhook público).

## Operação

```bash
docker compose logs -f app                       # logs
docker compose exec app transformsite ingest     # re-ler o site (agende no cron: diário)
docker compose exec app transformsite report     # métricas
docker compose exec app transformsite privacy purge   # retenção LGPD (agende no cron)
```

Cron sugerido (`crontab -e`):

```
0 3 * * * cd /opt/transformsite/deploy/vps && docker compose exec -T app transformsite ingest >/dev/null
30 3 * * * cd /opt/transformsite/deploy/vps && docker compose exec -T app transformsite privacy purge >/dev/null
```

Backup: a pasta `projeto/` (config, serviços, `kb/`, `data/`) é tudo que importa.
