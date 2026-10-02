# FALHAS — transformsite

| data | o que quebrou | menor correção | prompt \| infra |
|---|---|---|---|
| 2026-10-02 | horário "de carona" (dado junto com outro slot) não passava pelo validador da agenda → confirmava sábado e falhava só no fim | validar todo slot preenchido com `validate_tool` dentro de `_next` + cenário de regressão | infra |
| 2026-10-02 | eval do golden: 13 primeiras perguntas com HTTP 500 do Ollama (modelo recarregando) → contaram como "não sei" | retry com backoff em 5xx/conexão no cliente LLM (`_post_retry`) | infra |
| 2026-10-02 | 1º agendamento de ponta a ponta falhou: `Registry.call() got multiple values for argument 'name'` | parâmetro da ferramenta virou posicional-only (`call(tool, ctx, /, **args)`) | infra |
| 2026-10-02 | banner grade EN com "appoiintments" (texto inventado pelo gerador de imagem) | regerar com frase mais curta ("schedules meetings") | prompt |
