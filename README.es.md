# transformsite

[![transformsite — tu sitio se vuelve chat](guia/assets/banner-es.jpg)](https://inematds.github.io/transformsite/guia/es/)

**🇧🇷 [Português](README.md) · 🇺🇸 [English](README.en.md) · 🇪🇸 [Español](README.es.md)**

**Convierte tu sitio y tu base de conocimiento en un agente de atención por chat.**
El sitio es la fuente de verdad; el cliente conversa por **WhatsApp, Telegram, correo o widget en el sitio**.
El agente **responde citando la fuente** (o dice "no sé" y pasa con el equipo) y **ejecuta servicios**
(agendar, contacto, duplicado de factura…) recopilando los datos por conversación, en lugar de formularios.

## 📖 Guía de uso

Guía completa (landing + paso a paso): **https://inematds.github.io/transformsite/guia/es/** · 🗺️ [Roadmap](ROADMAP.md) (en portugués)

> Inspiración: **America.gov** (2026) reunió el contenido de ~29 mil sitios del gobierno estadounidense en un único
> punto de entrada conversacional — primero preguntas con fuente, después transacciones. transformsite es la
> versión abierta y simple de esa idea para cualquier empresa, escuela u organismo.

## Cómo funciona

```
sitio + docs ──ingest──► índice (BM25 + vectores) ──► RAG con citas ───┐
formularios legados ──convert──► services/*.yaml ──► máquina de slots ─┼──► Telegram · WhatsApp · correo · widget
                              herramientas (agenda, tickets, POST al legado, webhook) ┘
                              derivación humana + panel admin + métricas
```

- **Todo local por defecto**: LLM en [Ollama](https://ollama.com) (o vLLM/LM Studio), embeddings `bge-m3`,
  índice en un único archivo SQLite. También funciona con **Claude Code** (`claude -p`) o **Codex CLI**
  (`codex exec`) con tu suscripción — sin clave de API.
- **Servicio = un archivo YAML.** Sin código por servicio: slots con tipos validados (CPF, correo, teléfono,
  CEP, fecha/hora en portugués…), confirmación, idempotencia, traspaso a humano.
- **El legado sigue vivo**: el conversor genera servicios que hacen el mismo POST que hacía el formulario antiguo.
- **LGPD**: PII enmascarada en logs, retención configurable, `privacy forget`, OTP antes de datos sensibles.

## Instalación rápida (5 minutos)

```bash
pipx install git+https://github.com/inematds/transformsite      # o: pip install .
ollama pull qwen3.6:35b-a3b && ollama pull bge-m3                # cualquier modelo instruct sirve (p. ej.: qwen2.5:7b)

transformsite init meubot --org "Minha Empresa" --url https://www.minhaempresa.com.br --contact atendimento@minhaempresa.com.br
cd meubot
transformsite inventory          # Fase 0: páginas y formularios del sitio → inventario/*.csv
transformsite ingest             # Fase 1: lee todo el sitio + docs/ → kb/index.sqlite
transformsite chat               # conversación en la terminal
transformsite serve              # widget en /chat, panel en /admin, canales configurados
```

¿Sin GPU? Usa un modelo más pequeño (`qwen2.5:7b`, `llama3.1:8b`) o `llm.provider: claude_cli` / `codex_cli`.

## Comandos

| Comando | Qué hace |
|---|---|
| `init <carpeta>` | crea el proyecto (config, 2 servicios de ejemplo, escenarios de prueba) |
| `inventory` | rastrea el sitio y lista páginas y formularios con prioridad sugerida |
| `ingest` | crawler (sitemap + enlaces, respeta robots.txt) + docs locales → índice híbrido; reingesta incremental por hash |
| `ask "pregunta"` / `chat` | prueba en la terminal |
| `golden` | genera un borrador de preguntas de prueba a partir de la base (para revisar) |
| `eval` | mide: cita válida, "no sé" fuera de alcance, inyección bloqueada, latencia; ejecuta escenarios de conversación |
| `validate` | valida `services/*.yaml` (esquema + reglas: confirmación, idempotencia, herramientas existentes) |
| `convert form.html\|form.pdf\|URL` | formulario heredado → `service.yaml` en `review: pending` |
| `serve` | servidor: widget web, webhooks (WhatsApp/Telegram), pollers (Telegram/IMAP), panel |
| `report --since 7` | métricas (resolución sin humano, traspaso, tiempo hasta completar, abandono por slot) en JSON/CSV |
| `privacy forget --user canal:id` / `privacy purge` | LGPD: borrar un usuario / purgar por retención |

## Un servicio

```yaml
service: agendar
review: approved            # los servicios convertidos nacen "pending" y solo suben tras aprobación
intent:
  description: "Agendar um atendimento"
  examples: ["quero agendar", "marcar horário"]
slots:
  - {name: nome, type: text, prompt: "Qual é o seu nome?"}
  - {name: email, type: email, prompt: "Qual e-mail para o convite?"}
  - name: data_hora
    type: datetime
    prompt: "Qual dia e horário prefere?"
    options_from: tool:agenda.slots_livres          # ofrece horarios libres como botones
    constraints: {validate_tool: agenda.validar_horario}
confirm: {template: "Confirmo {nome} em {data_hora|fmt_br}?"}
action:
  tool: agenda.criar_evento
  args: {title: "Atendimento – {nome}", start: "{data_hora}", email: "{email}"}
  idempotency_key: "{session_id}:{data_hora}"
on_success: {reply: "Agendado! Protocolo {result.id}."}
```

El cliente puede responder todo de una vez ("sou a Ana, ana@x.com, quinta às 10"), corregir ("não, o e-mail
é…"), preguntar algo a mitad de camino (el bot responde con fuente y vuelve al trámite), cancelar o volver días después.

Herramientas propias: crea `tools/minha.py` con `register(registry)` — mira `tools/exemplo.py`.

## Canales

| Canal | Cómo activarlo |
|---|---|
| Widget web | activado por defecto: `<script src="https://SEU_DOMINIO/widget.js"></script>` |
| Telegram | `TELEGRAM_BOT_TOKEN` en el `.env` (crea el bot en @BotFather) |
| WhatsApp (Evolution API, self-hosted) | `channels.whatsapp` + `EVOLUTION_API_KEY`; webhook `POST /webhook/whatsapp` |
| WhatsApp (Cloud API oficial) | `channels.whatsapp_cloud` (`phone_number_id`, `token_env`, `verify_token`); webhook `/webhook/whatsapp-cloud` |
| Correo | `channels.email.enabled: true` + IMAP/SMTP |

Traspaso a humano: `handoff.targets` (`panel`, `email:...`, `telegram:<chat_id>`). En el panel, un agente
**toma** la conversación (el bot calla) y la **devuelve** al terminar (el bot retoma donde se quedó).

## Deploy en un VPS

```bash
curl -fsSL https://raw.githubusercontent.com/inematds/transformsite/main/deploy/vps/install.sh | sudo bash -s -- \
  --domain bot.suaempresa.com.br --site https://www.suaempresa.com.br --org "Sua Empresa" --contact atendimento@suaempresa.com.br --whatsapp
```

Levanta con Docker Compose: **app + Ollama + Evolution API (WhatsApp) + Caddy (HTTPS automático)**. Detalles en
[`deploy/vps/README.md`](deploy/vps/README.md) (en portugués).

## Piloto: INEMA.CLUB

La carpeta [`pilot/`](pilot/) es el proyecto real usado para validar el framework con el sitio
[inema.club](https://www.inema.club): 386 páginas → 1.512 fragmentos; con 73 preguntas de prueba y LLM local,
**92,6%** de respuestas con fuente válida, **100%** de "no sé" fuera de alcance, **100%** de inyección
bloqueada, p95 de 2,6 s; **13/13** guiones de conversación. Detalles en [`pilot/RESULTADOS.md`](pilot/RESULTADOS.md) (en portugués).

## Desarrollo

```bash
uv venv && uv pip install -e '.[dev]'
pytest -q                       # sin red, LLM falso determinista
```

Licencia MIT · hecho por [INEMA](https://www.inema.club) — contenido abierto y gratuito.
