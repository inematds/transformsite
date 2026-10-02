# Roadmap — transformsite

> Tese: o site e a base de conhecimento viram **a fonte de verdade**; o atendimento acontece em **um canal de conversa**
> (WhatsApp, Telegram, e-mail ou widget). Formulários e fluxos legados viram **serviços conversacionais**.
> Inspiração: America.gov (2026) — um único ponto de entrada conversacional sobre milhares de sites, começando por
> perguntas com fonte e evoluindo para transações.

## v1 — Framework (este repositório) ✅

| Fase | Entrega | Status |
|---|---|---|
| F0 Descoberta | `transformsite inventory` → `inventario/paginas.csv` + `formularios.csv` com prioridade | ✅ |
| F1 Perguntas com fonte | `ingest` (site + sitemap + docs locais) → índice híbrido BM25 + vetores (bge-m3) → RAG que cita a fonte e diz "não sei" | ✅ |
| F2 Serviços declarativos | `services/*.yaml` (schema `service/1`), roteador, máquina de slots (multi-dado, correção, cancelar, retomar, dúvida no meio, confirmação, idempotência), agenda local, tickets | ✅ |
| F3 Multicanal | Telegram, WhatsApp (Evolution API self-hosted e Cloud API oficial), e-mail (IMAP/SMTP), widget web — mesmos cenários em todos | ✅ |
| F4 Conversor | `transformsite convert form.html|form.pdf|URL` → `service.yaml` (`review: pending`) que continua enviando ao sistema legado | ✅ |
| F5 Operação | painel admin (conversas, assumir/devolver, fila, serviços, KB, métricas) + `transformsite report` | ✅ |
| F6 Empacotamento | `pip install`, `transformsite init`, Dockerfile, `deploy/vps` (app + Ollama + Evolution + Caddy/HTTPS), guia PT/EN/ES | ✅ |

## v2 — "Bot pronto em uma imagem" (próximo)

**Objetivo:** depois de puxar toda a base de conhecimento e definir os serviços, gerar **uma imagem Docker pronta**
que qualquer pessoa sobe com um comando — sem instalar Python, sem rodar ingest na produção.

| Item | Descrição | Critério de pronto |
|---|---|---|
| `transformsite bake` | Empacota projeto + `kb/index.sqlite` + `services/` aprovados + `tools/` numa imagem `ghcr.io/<org>/<bot>:<data>` | `docker run -p 8000:8000 <imagem>` responde `/health` com `kb.chunks > 0` e serviços carregados, sem rede para o site |
| Manifesto de entrega | `bake` gera `ENTREGA.md`: páginas indexadas, serviços entregues (com versão), canais habilitados, data do crawl, hash do índice | arquivo gerado e embutido na imagem (`/entrega`) |
| Catálogo de serviços | definição dos serviços a entregar escolhida por checklist (`transformsite services pick`) a partir do inventário + templates | YAMLs gerados, aprovados e validados antes do bake |
| Atualização incremental | `bake --since <tag>` só re-ingere páginas alteradas (hash) e publica nova tag | diff de páginas no manifesto |
| Re-crawl agendado | cron/webhook de deploy do site dispara `ingest` + `bake` | doc + exemplo GitHub Actions |
| Imagem com modelo | variante `-full` com Ollama + modelo embutido (para máquinas offline) | sobe sem internet |

## v3 — Transações e identidade

- Autenticação nível 3 (login do sistema legado / gov.br para órgãos públicos) além do OTP por e-mail.
- Serviços com documento: anexos (foto/PDF) no WhatsApp, OCR local, geração de PDF preenchido.
- Pagamentos/boletos via ferramenta do legado (`legado.*`) com confirmação dupla.
- Áudio do WhatsApp → texto (Whisper local) e resposta em áudio (TTS local).

## v4 — Escala e multi-tenant

- Postgres + pgvector, fila (Redis) e múltiplos workers; vLLM para throughput.
- Vários bots por instalação (um por cliente), painel multiusuário com papéis.
- Avaliação contínua: golden set rodando a cada re-crawl; alerta quando `citacao_valida` cair.
- Trilíngue PT/EN/ES de ponta a ponta (KB e respostas), aproveitando os relatórios do projeto WiFi.

## Fora de escopo (por decisão)

- Usar API paga de LLM por padrão — o padrão é LLM local ou assinatura (Claude Code / Codex CLI).
- Substituir o sistema legado: o bot **embrulha** o legado (POST no formulário antigo, webhook, API), não vira o banco mestre.
