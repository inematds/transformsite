"""Canal Telegram (Bot API via httpx).

- Entrada: `update` com `message` (texto) ou `callback_query` (clique em botão inline).
- Saída: `sendMessage` com inline keyboard (até 8 opções, uma por linha); acima disso, texto numerado.
- Recebimento: long polling (`run_poller`) ou webhook (`router` → POST /webhook/telegram).
- Mensagens vindas de grupos são ignoradas (o grupo serve para avisos de handoff à equipe).
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from ..messages import Capabilities, InMsg, OutMsg, render_text
from . import Channel

log = logging.getLogger(__name__)

MAX_TEXT = 4000
MAX_CALLBACK = 64  # bytes de callback_data aceitos pelo Telegram


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Divide texto longo preferindo quebras de linha/espaço."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, cfg=None, http: httpx.Client | None = None):
        self.cfg = cfg
        self.caps = Capabilities(buttons=8, lists=0, markdown=False, files=True)
        self._http = http

    # ---------- config (resolvida só quando há rede envolvida) ----------
    @property
    def _conf(self) -> dict:
        return dict(self.cfg.channels.telegram) if self.cfg is not None else {}

    @property
    def api_base(self) -> str:
        return str(self._conf.get("api_base") or "https://api.telegram.org").rstrip("/")

    @property
    def token(self) -> str:
        env = self._conf.get("token_env", "TELEGRAM_BOT_TOKEN")
        tok = os.environ.get(env, "")
        if not tok:
            raise RuntimeError(f"token do Telegram ausente (variável {env})")
        return tok

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=40)
        return self._http

    # ---------- entrada ----------
    def to_inbound(self, payload: dict) -> InMsg | None:
        if "callback_query" in payload:
            cq = payload["callback_query"]
            msg = cq.get("message") or {}
            chat = (msg.get("chat") or {}).get("id") or (cq.get("from") or {}).get("id")
            if chat is None or (msg.get("chat") or {}).get("type", "private") != "private":
                return None
            data = cq.get("data")
            label = None
            for row in ((msg.get("reply_markup") or {}).get("inline_keyboard") or []):
                for b in row:
                    if b.get("callback_data") == data:
                        label = b.get("text")
            frm = cq.get("from") or {}
            return InMsg(
                self.name,
                str(chat),
                label or str(data or ""),
                msg_id=f"tgcb:{cq.get('id')}",
                choice_id=data,
                meta=self._meta(frm),
            )
        msg = payload.get("message")
        if not msg:
            return None  # edited_message, channel_post, my_chat_member... ignorados
        chat = msg.get("chat") or {}
        if chat.get("type", "private") != "private":
            return None
        text = msg.get("text") or msg.get("caption")
        if not text:
            return None
        return InMsg(
            self.name,
            str(chat.get("id")),
            text,
            msg_id=f"tg:{chat.get('id')}:{msg.get('message_id')}",
            meta=self._meta(msg.get("from") or {}),
        )

    @staticmethod
    def _meta(frm: dict) -> dict:
        meta: dict[str, Any] = {}
        if frm.get("first_name"):
            meta["name"] = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
        if frm.get("language_code"):
            meta["lang"] = frm["language_code"]
        if frm.get("username"):
            meta["username"] = frm["username"]
        return meta

    # ---------- saída ----------
    def to_payloads(self, user_id: str, out: OutMsg) -> list[dict]:
        caps = self.caps
        if any(len(str(c["id"]).encode()) > MAX_CALLBACK for c in out.choices):
            caps = Capabilities(buttons=0, files=caps.files)  # id não cabe em callback_data → numerado
        text, choices, mode = render_text(out, caps)
        chunks = split_text(text)
        payloads: list[dict] = [{"method": "sendMessage", "chat_id": user_id, "text": c} for c in chunks]
        if mode == "buttons":
            payloads[-1]["reply_markup"] = {
                "inline_keyboard": [[{"text": c["label"], "callback_data": str(c["id"])}] for c in choices]
            }
        for a in out.attachments:
            if a.get("url"):
                payloads.append({"method": "sendDocument", "chat_id": user_id, "document": a["url"]})
        return payloads

    def payload_text(self, payload: dict) -> str:
        if payload.get("method") == "sendDocument":
            return f"[arquivo] {payload.get('document', '')}"
        text = payload.get("text", "")
        kb = (payload.get("reply_markup") or {}).get("inline_keyboard") or []
        labels = [b["text"] for row in kb for b in row]
        if labels:
            text += "\n" + " ".join(f"[{lab}]" for lab in labels)
        return text

    def call(self, method: str, data: dict) -> dict:
        r = self.http.post(f"{self.api_base}/bot{self.token}/{method}", json=data)
        body = r.json() if r.content else {}
        if r.status_code >= 400 or not body.get("ok", False):
            raise RuntimeError(f"Telegram {method} falhou: HTTP {r.status_code} {str(body.get('description', ''))[:200]}")
        return body

    def send(self, user_id: str, out: OutMsg) -> None:
        for p in self.to_payloads(user_id, out):
            p = dict(p)
            self.call(p.pop("method"), p)

    def native_inbound(self, user_id, text, meta=None, choice=None, msg_id=None):
        chat = {"id": user_id, "type": "private"}
        frm = {"id": user_id, "first_name": (meta or {}).get("name", "Cliente"), "language_code": "pt-br"}
        if choice:
            return {
                "update_id": 1,
                "callback_query": {
                    "id": msg_id or uuid.uuid4().hex,
                    "from": frm,
                    "data": choice,
                    "message": {"message_id": 1, "chat": chat, "reply_markup": {"inline_keyboard": []}},
                },
            }
        return {"update_id": 1, "message": {"message_id": msg_id or uuid.uuid4().hex, "chat": chat, "from": frm, "text": text}}

    # ---------- integração com o servidor ----------
    def process_update(self, agent, update: dict) -> None:
        if "callback_query" in update:
            try:
                self.call("answerCallbackQuery", {"callback_query_id": update["callback_query"].get("id")})
            except Exception as e:  # botão fica "carregando", mas a conversa segue
                log.warning("answerCallbackQuery: %s", e)
        msg = self.to_inbound(update)
        if msg is None:
            return
        for out in agent.handle(msg):
            self.send(msg.user_id, out)

    def run_poller(self, agent, stop_event) -> None:
        """Long polling getUpdates. Erros de rede → backoff exponencial (até 60s)."""
        offset = None
        backoff = 1.0
        try:  # webhook ativo impede getUpdates
            self.call("deleteWebhook", {"drop_pending_updates": False})
        except Exception as e:
            log.warning("deleteWebhook: %s", e)
        while not stop_event.is_set():
            try:
                data: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
                if offset is not None:
                    data["offset"] = offset
                updates = self.call("getUpdates", data).get("result", [])
                backoff = 1.0
            except Exception as e:
                log.warning("telegram getUpdates: %s (nova tentativa em %.0fs)", e, backoff)
                stop_event.wait(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            for up in updates:
                offset = up["update_id"] + 1
                try:
                    self.process_update(agent, up)
                except Exception as e:  # uma mensagem ruim não derruba o poller
                    log.exception("telegram update %s: %s", up.get("update_id"), e)

    def attach(self, agent) -> None:
        """Registra envio de respostas humanas (painel) e avisos de handoff para chats/grupos."""

        def hook(sess: dict, out: OutMsg) -> None:
            if sess and sess.get("channel") == self.name:
                self.send(sess["user_id"], out)

        agent.outbound_hooks.append(hook)
        agent.notifier.senders[self.name] = lambda chat_id, text: self.send(chat_id, OutMsg(text))

    def router(self, agent):
        r = APIRouter()
        secret_env = self._conf.get("webhook_secret_env", "TELEGRAM_WEBHOOK_SECRET")

        @r.post("/webhook/telegram")
        def telegram_webhook(update: dict, request: Request, bg: BackgroundTasks):
            secret = os.environ.get(secret_env, "")
            if secret and request.headers.get("x-telegram-bot-api-secret-token") != secret:
                raise HTTPException(403, "segredo inválido")
            bg.add_task(self.process_update, agent, update)
            return {"ok": True}

        return r
