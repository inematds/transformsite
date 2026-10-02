# Goal — framework

- **Início:** 2026-10-02 10:04 · **Agente:** Claude Code (Opus 5.5) · plano escrito pelo Fable 5.1 (`PLANO.md`)
- **Tetos:** tempo ~8 h · memória de processo 8G · saídas de ferramenta curtas

## Resultado
Repo público `inematds/transformsite`: framework Python que qualquer pessoa baixa e aplica
para transformar site + base de conhecimento em um agente de chat (Telegram, WhatsApp/Evolution,
e-mail, widget web) que responde com citação, executa serviços declarativos (agendar, fale conosco…),
converte formulários legados em serviços e tem painel admin; piloto = inema.club; deploy VPS pronto;
ROADMAP com v2 (imagem Docker "assada" com KB + serviços); publicado no portal.

## Critérios de pronto (verificáveis)

**Função**
- [ ] F0 `transformsite inventory --url https://www.inema.club --max-pages 60` → `inventario/paginas.csv` ≥ 30 linhas e `inventario/formularios.csv` existe
- [ ] F0 `wc -l pilot/tests/golden.jsonl` ≥ 65, com ≥10 `fora_de_escopo`, ≥2 `injecao`; respostas tiradas das páginas ingeridas
- [ ] F1 `transformsite ingest` no piloto → `kb/index.sqlite` com chunks ≥ 200 e embeddings bge-m3
- [ ] F1 `transformsite eval --golden tests/golden.jsonl` (LLM real Ollama) → citacao_valida ≥ 0.85, nao_sei_fora_escopo ≥ 0.90, injecao_bloqueada = 1.0 — números REAIS colados
- [ ] F2 `transformsite validate services/` → 0 erros; serviço `review: pending` recusado pelo serve (teste)
- [ ] F2 `transformsite eval --scenarios tests/cenarios/` → 8 roteiros de agendar passam (feliz, multi-slot, correção, cancelar, retomar, dúvida no meio, horário inválido, duplo envio = 1 evento)
- [ ] F3 conformance: mesmos cenários passam via adaptadores telegram, evolution(whatsapp), email, web (servidores fake locais); 5 opções → lista/numerado conforme canal
- [ ] F4 `transformsite convert` em ≥10 formulários fixture (HTML + AcroForm PDF) → YAML que passa `validate`, ≥80% slots corretos vs gabarito, nasce `review: pending`
- [ ] F5 painel: takeover → bot silencia; release → bot retoma (teste); `transformsite report --since 7d` gera CSV
- [ ] F6 `uv build` ok; instalação limpa num venv novo `pip install dist/*.whl && transformsite init demo && transformsite chat` funciona; `deploy/vps/docker-compose.yml` (app + evolution + ollama + caddy) passa `docker compose config`
- [ ] README PT/EN/ES + guia/index.html (+en/es); ROADMAP.md com v2

**Regressão**
- [ ] `pytest -q` verde (testes sem rede, LLM fake), contagem de testes ≥ 60

**Limite**
- [ ] nenhuma chamada a API externa paga; nenhuma chave no repo (`git grep -nE "sk-|bot[0-9]{6,}:"` vazio)
- [ ] Telegram/SMTP/Evolution ao vivo NÃO rodados sem token + autorização explícita (registrado em LIMITES.md)

**Teste rápido por ciclo**: `pytest -q -x`
**Teste completo no final**: `pytest -q && transformsite eval` no piloto

## Restrições
- só local/assinatura (Ollama, claude -p, codex exec); sem API sem autorização
- autor git: inematds <inematds@gmail.com>

## Portões humanos
- token do bot Telegram + OK para Telegram Bot API (teste ao vivo)
- credenciais SMTP / Evolution reais
