"""Adaptadores de canal.

Todo canal implementa a mesma interface (`Channel`), o que permite rodar os MESMOS cenários
em todos eles (conformance) usando payloads nativos simulados — sem rede.

- `to_inbound(payload)`      payload nativo recebido (webhook/update) → InMsg (ou None p/ ignorar)
- `to_payloads(user, out)`   OutMsg → lista de payloads nativos de envio (degradação de botões)
- `send(user, out)`          envia de verdade (rede)
- `native_inbound(...)`      constrói um payload nativo fake (simulação/testes)
- `payload_text(payload)`    texto visível de um payload de envio (para asserções)
"""

from __future__ import annotations

import importlib

from ..messages import Capabilities, InMsg, OutMsg, render_text


class Channel:
    name = "base"
    caps = Capabilities()

    def to_inbound(self, payload: dict) -> InMsg | None:
        raise NotImplementedError

    def to_payloads(self, user_id: str, out: OutMsg) -> list[dict]:
        raise NotImplementedError

    def send(self, user_id: str, out: OutMsg) -> None:
        raise NotImplementedError

    def native_inbound(self, user_id: str, text: str, meta: dict | None = None, choice: str | None = None, msg_id: str | None = None) -> dict:
        raise NotImplementedError

    def payload_text(self, payload: dict) -> str:
        raise NotImplementedError

    # ---- simulação ----
    def exchange(self, agent, user_id, text, meta=None, choice=None, msg_id=None) -> list[str]:
        payload = self.native_inbound(user_id, text, meta or {}, choice, msg_id)
        msg = self.to_inbound(payload)
        if msg is None:
            return []
        outs = agent.handle(msg)
        return [self.payload_text(p) for o in outs for p in self.to_payloads(user_id, o)]


class CLIChannel(Channel):
    name = "cli"
    caps = Capabilities(buttons=0, lists=0)

    def to_inbound(self, payload):
        return InMsg("cli", payload["user"], payload["text"], msg_id=payload.get("id"), choice_id=payload.get("choice"), meta=payload.get("meta", {}))

    def to_payloads(self, user_id, out):
        text, _, _ = render_text(out, self.caps)
        return [{"text": text}]

    def send(self, user_id, out):
        print(self.to_payloads(user_id, out)[0]["text"])

    def native_inbound(self, user_id, text, meta=None, choice=None, msg_id=None):
        return {"user": user_id, "text": text, "meta": meta or {}, "choice": choice, "id": msg_id}

    def payload_text(self, payload):
        return payload["text"]


_REGISTRY = {
    "cli": ("transformsite.channels", "CLIChannel"),
    "telegram": ("transformsite.channels.telegram", "TelegramChannel"),
    "whatsapp": ("transformsite.channels.whatsapp", "EvolutionChannel"),
    "whatsapp_cloud": ("transformsite.channels.whatsapp", "CloudAPIChannel"),
    "email": ("transformsite.channels.email", "EmailChannel"),
    "web": ("transformsite.channels.web", "WebChannel"),
}


def get_channel(name: str, cfg=None) -> Channel:
    if name not in _REGISTRY:
        raise KeyError(f"canal desconhecido: {name} (disponíveis: {', '.join(_REGISTRY)})")
    mod, cls = _REGISTRY[name]
    klass = getattr(importlib.import_module(mod), cls)
    try:
        return klass(cfg)
    except TypeError:
        return klass()


def get_simulator(name: str) -> Channel:
    return get_channel(name, None)


CHANNELS = list(_REGISTRY)
