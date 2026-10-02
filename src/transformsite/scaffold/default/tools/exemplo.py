"""Exemplo de ferramenta do projeto. Renomeie/edite à vontade.

Toda função recebe `ctx` (cfg, store, session, service) + os args do `action.args` do serviço,
e devolve um dict — acessível nas respostas como {result.<campo>}.
"""


def consultar_pedido(ctx, numero: str, **_):
    # Troque por uma chamada ao seu sistema (REST, banco, planilha…).
    return {"id": numero, "status": "em separação"}


def register(registry):
    registry.add("exemplo.consultar_pedido", consultar_pedido, side_effects=False, description="Consulta status de pedido")
