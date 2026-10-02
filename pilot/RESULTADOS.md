# Piloto INEMA.CLUB — resultados (2026-10-02)

Projeto real do framework aplicado ao site [inema.club](https://www.inema.club).
LLM local: Ollama `qwen3.6:35b-a3b` (chat) + `bge-m3` (embeddings), numa GPU NVIDIA GB10.

## F0 — Inventário

```
transformsite inventory --sitemap https://www.inema.club/sitemap.xml --sitemap https://www.inema.club/courses-sitemap.xml
```

| | |
|---|---|
| Páginas | **387** (`inventario/paginas.csv`; versões /en/ e /es/ excluídas por config) |
| Formulários | **1** (busca de cursos) — o site não tem formulários legados; a F4 foi medida com 13 formulários de exemplo em `tests/fixtures/forms` |

## F1 — Base de conhecimento e respostas com fonte

```
transformsite ingest         → 386 páginas, 1.512 trechos, 1.512 embeddings, 249 s
transformsite eval --golden tests/golden.jsonl
```

Golden set: **73 perguntas** (`tests/golden.jsonl`) — 54 respondíveis pela base (geradas por
`transformsite golden` a partir de trechos reais e revisadas à mão), 12 fora de escopo, 4 tentativas de
injeção de prompt, 3 sobre versões diferentes do mesmo curso.

| Métrica | Resultado | Meta |
|---|---|---|
| Citação válida (resposta com ≥1 URL existente no índice) | **92,6%** | ≥ 85% ✅ |
| Fonte exatamente a página esperada | 88,9% | — |
| Resposta contém os termos-chave esperados | 70,4% | — (indicativo) |
| "Não sei" em pergunta fora de escopo | **100%** | ≥ 90% ✅ |
| Injeção bloqueada | **100%** | 100% ✅ |
| Versões diferentes sinalizadas com fonte | 66,7% | — |
| Latência p50 / p95 | 1,97 s / 2,64 s | p95 < 10 s ✅ |
| Erros técnicos | 0 | — |

Detalhe por pergunta: `tests/resultados/golden-2026-10-02.jsonl`.

**Histórico da medição** (o que mudou entre a 1ª e a 2ª rodada):

| Rodada | Citação válida | Causa / correção |
|---|---|---|
| 1ª | 66,7% | 13 erros HTTP 500 do Ollama (modelo recarregando) contaram como "não sei" → retry com backoff no cliente LLM. Seções curtas (ex.: "Para quem é") ficavam fora do top-k por causa do limite de 2 trechos por página e de "quem" ser stopword → até 3 trechos por página + expansão da página líder + stopwords ajustadas |
| 2ª | **92,6%** | — |

## F2 — Serviços e conversa

Serviços do piloto: `agendar` (conversa com a equipe INEMA), `fale_conosco`, `sugerir_curso`.

```
transformsite eval --scenarios tests/cenarios              → 13/13 (LLM falso, determinístico)
transformsite eval --scenarios tests/cenarios --real-llm   → 13/13 (Ollama qwen3.6)
```

Roteiros: caminho feliz, vários dados numa frase, correção na confirmação, cancelar, retomar após 2 dias,
dúvida no meio do fluxo, horário inválido, mensagem duplicada (1 só agendamento), fale conosco, pedido de
humano, aviso de dado sensível, sugerir curso, horário inválido chegando "de carona".

## F3 — Canais

Conformance: os mesmos cenários passam em **cli, Telegram, WhatsApp (Evolution), WhatsApp (Cloud API),
e-mail e widget web** com payloads nativos simulados (`tests/test_channels.py`).
Ao vivo: widget web + painel verificados com o LLM real (ver capturas no guia).
**Pendente de autorização:** Telegram/WhatsApp/SMTP reais exigem token/instância e autorização para uso das APIs.

## Como reproduzir

```bash
cd pilot
transformsite ingest
transformsite eval --golden tests/golden.jsonl
transformsite eval --scenarios tests/cenarios --real-llm
transformsite serve --channels web     # http://localhost:8000/chat e /admin
```
