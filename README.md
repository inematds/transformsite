# transformsite

**Transforme seu site e sua base de conhecimento em um agente de atendimento por chat.**
O site vira a fonte de verdade; o cliente conversa por **WhatsApp, Telegram, e-mail ou widget no site**.
O agente **responde citando a fonte** (ou diz "não sei" e passa para a equipe) e **executa serviços**
(agendar, fale conosco, segunda via…) coletando os dados por conversa, em vez de formulários.

🇧🇷 Português · [🇺🇸 English](README.en.md) · [🇪🇸 Español](README.es.md) · 📘 [Guia](https://inematds.github.io/transformsite/guia/) · 🗺️ [Roadmap](ROADMAP.md)

> Inspiração: o **America.gov** (2026) juntou o conteúdo de ~29 mil sites do governo americano num único
> ponto de entrada conversacional — primeiro perguntas com fonte, depois transações. O transformsite é a
> versão aberta e simples dessa ideia para qualquer empresa, escola ou órgão.

## Como funciona

```
site + docs ──ingest──► índice (BM25 + vetores) ──► RAG com citação ─┐
formulários legados ──convert──► services/*.yaml ──► máquina de slots ┼──► Telegram · WhatsApp · e-mail · widget
                                         ferramentas (agenda, tickets, POST no legado, webhook) ┘
                                         handoff humano + painel admin + métricas
```

- **Tudo local por padrão**: LLM no [Ollama](https://ollama.com) (ou vLLM/LM Studio), embeddings `bge-m3`,
  índice num único arquivo SQLite. Também funciona com **Claude Code** (`claude -p`) ou **Codex CLI**
  (`codex exec`) pela sua assinatura — sem chave de API.
- **Serviço = um arquivo YAML.** Sem código por serviço: slots com tipos validados (CPF, e-mail, telefone,
  CEP, data/hora em português…), confirmação, idempotência, handoff.
- **O legado continua vivo**: o conversor gera serviços que fazem o mesmo POST que o formulário antigo fazia.
- **LGPD**: PII mascarada em logs, retenção configurável, `privacy forget`, OTP antes de dados sensíveis.

## Instalação rápida (5 minutos)

```bash
pipx install git+https://github.com/inematds/transformsite      # ou: pip install .
ollama pull qwen3.6:35b-a3b && ollama pull bge-m3                # qualquer modelo instruct serve (ex.: qwen2.5:7b)

transformsite init meubot --org "Minha Empresa" --url https://www.minhaempresa.com.br --contact atendimento@minhaempresa.com.br
cd meubot
transformsite inventory          # Fase 0: páginas e formulários do site → inventario/*.csv
transformsite ingest             # Fase 1: lê o site inteiro + docs/ → kb/index.sqlite
transformsite chat               # conversa no terminal
transformsite serve              # widget em /chat, painel em /admin, canais configurados
```

Sem GPU? Use um modelo menor (`qwen2.5:7b`, `llama3.1:8b`) ou `llm.provider: claude_cli` / `codex_cli`.

## Comandos

| Comando | O que faz |
|---|---|
| `init <pasta>` | cria o projeto (config, 2 serviços de exemplo, cenários de teste) |
| `inventory` | rastreia o site e lista páginas e formulários com prioridade sugerida |
| `ingest` | crawler (sitemap + links, respeita robots.txt) + docs locais → índice híbrido; reingestão incremental por hash |
| `ask "pergunta"` / `chat` | testa no terminal |
| `golden` | gera rascunho de perguntas de teste a partir da base (para revisar) |
| `eval` | mede: citação válida, "não sei" fora de escopo, injeção bloqueada, latência; roda cenários de conversa |
| `validate` | valida `services/*.yaml` (schema + regras: confirmação, idempotência, ferramentas existentes) |
| `convert form.html\|form.pdf\|URL` | formulário legado → `service.yaml` em `review: pending` |
| `serve` | servidor: widget web, webhooks (WhatsApp/Telegram), pollers (Telegram/IMAP), painel |
| `report --since 7` | métricas (resolução sem humano, handoff, tempo até concluir, abandono por slot) em JSON/CSV |
| `privacy forget --user canal:id` / `privacy purge` | LGPD: apagar um usuário / expurgar por retenção |

## Um serviço

```yaml
service: agendar
review: approved            # serviços convertidos nascem "pending" e só sobem após aprovação
intent:
  description: "Agendar um atendimento"
  examples: ["quero agendar", "marcar horário"]
slots:
  - {name: nome, type: text, prompt: "Qual é o seu nome?"}
  - {name: email, type: email, prompt: "Qual e-mail para o convite?"}
  - name: data_hora
    type: datetime
    prompt: "Qual dia e horário prefere?"
    options_from: tool:agenda.slots_livres          # oferece horários livres como botões
    constraints: {validate_tool: agenda.validar_horario}
confirm: {template: "Confirmo {nome} em {data_hora|fmt_br}?"}
action:
  tool: agenda.criar_evento
  args: {title: "Atendimento – {nome}", start: "{data_hora}", email: "{email}"}
  idempotency_key: "{session_id}:{data_hora}"
on_success: {reply: "Agendado! Protocolo {result.id}."}
```

O cliente pode responder tudo de uma vez ("sou a Ana, ana@x.com, quinta às 10"), corrigir ("não, o e-mail
é…"), perguntar algo no meio (o bot responde com fonte e volta ao pedido), cancelar ou voltar dias depois.

Ferramentas próprias: crie `tools/minha.py` com `register(registry)` — veja `tools/exemplo.py`.

## Canais

| Canal | Como ativar |
|---|---|
| Widget web | ligado por padrão: `<script src="https://SEU_DOMINIO/widget.js"></script>` |
| Telegram | `TELEGRAM_BOT_TOKEN` no `.env` (crie o bot no @BotFather) |
| WhatsApp (Evolution API, self-hosted) | `channels.whatsapp` + `EVOLUTION_API_KEY`; webhook `POST /webhook/whatsapp` |
| WhatsApp (Cloud API oficial) | `channels.whatsapp.provider: cloud` + token Meta; webhook `/webhook/whatsapp-cloud` |
| E-mail | `channels.email.enabled: true` + IMAP/SMTP |

Handoff humano: `handoff.targets` (`panel`, `email:...`, `telegram:<chat_id>`). No painel, um atendente
**assume** a conversa (o bot silencia) e **devolve** quando terminar (o bot retoma de onde parou).

## Deploy numa VPS

```bash
curl -fsSL https://raw.githubusercontent.com/inematds/transformsite/main/deploy/vps/install.sh | sudo bash -s -- \
  --domain bot.suaempresa.com.br --site https://www.suaempresa.com.br --org "Sua Empresa" --contact atendimento@suaempresa.com.br --whatsapp
```

Sobe com Docker Compose: **app + Ollama + Evolution API (WhatsApp) + Caddy (HTTPS automático)**. Detalhes em
[`deploy/vps/README.md`](deploy/vps/README.md).

## Piloto: INEMA.CLUB

A pasta [`pilot/`](pilot/) é o projeto real usado para validar o framework com o site
[inema.club](https://www.inema.club): inventário, golden set e resultados de avaliação em
[`pilot/RESULTADOS.md`](pilot/RESULTADOS.md).

## Desenvolvimento

```bash
uv venv && uv pip install -e '.[dev]'
pytest -q                       # sem rede, LLM falso determinístico
```

Licença MIT · feito pelo [INEMA](https://www.inema.club) — conteúdo aberto e gratuito.
