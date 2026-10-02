"""Canais WhatsApp.

`EvolutionChannel` (Evolution API v2, self-hosted — padrão do projeto)
    Entrada: webhook `messages.upsert`. Saída: `POST {base_url}/message/sendText/{instance}` (header `apikey`).
    Botões: a Evolution usa Baileys (WhatsApp Web não oficial); botões/listas interativos NÃO têm entrega
    garantida (o WhatsApp descarta em muitas contas). Por isso o padrão é texto com opções numeradas
    (caps buttons=0, lists=0). `use_lists: true` em `channels.whatsapp` liga `sendList` (≤10 itens),
    por sua conta e risco. A resposta numerada ("2") é entendida pelo motor.

`CloudAPIChannel` (WhatsApp Cloud API oficial da Meta)
    Entrada: webhook `entry[].changes[].value.messages[]`. Saída: `POST {graph}/v20.0/{phone_number_id}/messages`.
    ≤3 opções → botões (título ≤20), ≤10 → lista (título ≤24), acima disso → texto numerado.
    GET de verificação com `verify_token`; POST validado com `X-Hub-Signature-256` se houver app secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ..messages import Capabilities, InMsg, OutMsg, render_text
from . import Channel
from .telegram import split_text

log = logging.getLogger(__name__)

MAX_TEXT = 4000


def _digits(s: str) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def _labels_line(labels: list[str]) -> str:
    return " ".join(f"[{lab}]" for lab in labels)


class _WABase(Channel):
    conf_key = "whatsapp"

    def __init__(self, cfg=None, http: httpx.Client | None = None):
        self.cfg = cfg
        self._http = http

    @property
    def _conf(self) -> dict:
        return dict(getattr(self.cfg.channels, self.conf_key)) if self.cfg is not None else {}

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=30)
        return self._http

    def _env(self, key: str, default_env: str, required: bool = True) -> str:
        env = self._conf.get(key, default_env)
        val = os.environ.get(env, "")
        if required and not val:
            raise RuntimeError(f"credencial do WhatsApp ausente (variável {env})")
        return val

    def send(self, user_id: str, out: OutMsg) -> None:
        for p in self.to_payloads(user_id, out):
            self._post(p)

    def _post(self, payload: dict) -> None:
        raise NotImplementedError

    def process(self, agent, payload: dict) -> None:
        for msg in self.to_inbounds(payload):
            for out in agent.handle(msg):
                self.send(msg.user_id, out)

    def to_inbounds(self, payload: dict) -> list[InMsg]:
        m = self.to_inbound(payload)
        return [m] if m else []

    def attach(self, agent) -> None:
        def hook(sess: dict, out: OutMsg) -> None:
            if sess and sess.get("channel") == self.name:
                self.send(sess["user_id"], out)

        agent.outbound_hooks.append(hook)
        # aviso de handoff para um número da equipe: handoff.targets: ["whatsapp:5551999990000"]
        agent.notifier.senders[self.name] = lambda dest, text: self.send(_digits(dest), OutMsg(text))


# ======================= Evolution API v2 =======================
class EvolutionChannel(_WABase):
    name = "whatsapp"

    def __init__(self, cfg=None, http: httpx.Client | None = None, use_lists: bool | None = None):
        super().__init__(cfg, http)
        if use_lists is None:
            use_lists = bool(self._conf.get("use_lists", False))
        self.use_lists = use_lists
        self.caps = Capabilities(buttons=0, lists=10 if use_lists else 0)

    # ---------- entrada ----------
    def to_inbound(self, payload: dict) -> InMsg | None:
        event = str(payload.get("event", "messages.upsert")).lower().replace("_", ".")
        if event != "messages.upsert":
            return None
        data = payload.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        key = data.get("key") or {}
        jid = str(key.get("remoteJid") or "")
        if not jid or key.get("fromMe") or jid.endswith("@g.us") or jid.endswith("@broadcast") or jid.endswith("@newsletter"):
            return None
        m = data.get("message") or {}
        text, choice = None, None
        if m.get("conversation"):
            text = m["conversation"]
        elif (m.get("extendedTextMessage") or {}).get("text"):
            text = m["extendedTextMessage"]["text"]
        elif m.get("buttonsResponseMessage"):
            b = m["buttonsResponseMessage"]
            choice = b.get("selectedButtonId")
            text = b.get("selectedDisplayText") or choice
        elif m.get("listResponseMessage"):
            lr = m["listResponseMessage"]
            choice = (lr.get("singleSelectReply") or {}).get("selectedRowId")
            text = lr.get("title") or choice
        elif m.get("templateButtonReplyMessage"):
            t = m["templateButtonReplyMessage"]
            choice = t.get("selectedId")
            text = t.get("selectedDisplayText") or choice
        else:
            for k in ("imageMessage", "videoMessage", "documentMessage"):
                if (m.get(k) or {}).get("caption"):
                    text = m[k]["caption"]
        if not text:
            return None  # áudio/figurinha/reação: sem texto (transcrição de áudio fica para outra etapa)
        user = jid.split("@", 1)[0].split(":", 1)[0]
        meta: dict[str, Any] = {}
        if jid.endswith("@s.whatsapp.net") and _digits(user):
            meta["phone"] = "+" + _digits(user)
        if data.get("pushName"):
            meta["name"] = data["pushName"]
        return InMsg(self.name, user, text, msg_id=key.get("id"), choice_id=choice, meta=meta)

    # ---------- saída ----------
    def to_payloads(self, user_id: str, out: OutMsg) -> list[dict]:
        text, choices, mode = render_text(out, self.caps)
        if mode == "list":
            return [
                {
                    "path": "sendList",
                    "number": user_id,
                    "title": "",
                    "description": text[:MAX_TEXT],
                    "buttonText": "Ver opções",
                    "footerText": "",
                    "sections": [
                        {"title": "Opções", "rows": [{"title": c["label"][:24], "description": c["label"][24:96], "rowId": str(c["id"])} for c in choices]}
                    ],
                }
            ]
        payloads = [{"path": "sendText", "number": user_id, "text": t} for t in split_text(text, MAX_TEXT)]
        for a in out.attachments:
            if a.get("url"):
                payloads.append({"path": "sendMedia", "number": user_id, "mediatype": "document", "media": a["url"], "fileName": a.get("name", "arquivo")})
        return payloads

    def payload_text(self, payload: dict) -> str:
        if payload.get("path") == "sendList":
            labels = [r["title"] + r.get("description", "") for s in payload["sections"] for r in s["rows"]]
            return payload["description"] + "\n" + _labels_line(labels)
        if payload.get("path") == "sendMedia":
            return f"[arquivo] {payload.get('media', '')}"
        return payload.get("text", "")

    def _post(self, payload: dict) -> None:
        base = str(self._conf.get("base_url") or "").rstrip("/")
        instance = self._conf.get("instance") or ""
        if not base or not instance:
            raise RuntimeError("configure channels.whatsapp.base_url e channels.whatsapp.instance (Evolution)")
        p = dict(payload)
        path = p.pop("path")
        r = self.http.post(f"{base}/message/{path}/{instance}", json=p, headers={"apikey": self._env("apikey_env", "EVOLUTION_API_KEY")})
        if r.status_code >= 400:
            raise RuntimeError(f"Evolution {path} falhou: HTTP {r.status_code} {r.text[:200]}")

    def native_inbound(self, user_id, text, meta=None, choice=None, msg_id=None):
        key = {"remoteJid": f"{_digits(user_id) or user_id}@s.whatsapp.net", "fromMe": False, "id": msg_id or uuid.uuid4().hex.upper()}
        if choice:
            message = {"listResponseMessage": {"title": text, "singleSelectReply": {"selectedRowId": choice}}}
        else:
            message = {"conversation": text}
        return {"event": "messages.upsert", "instance": "sim", "data": {"key": key, "pushName": (meta or {}).get("name", ""), "message": message, "messageType": "conversation"}}

    def router(self, agent):
        r = APIRouter()

        @r.post("/webhook/whatsapp")
        def whatsapp_webhook(payload: dict, request: Request, bg: BackgroundTasks):
            # opcional: token na URL do webhook (?token=...) definido em channels.whatsapp.webhook_token_env
            secret = self._env("webhook_token_env", "WHATSAPP_WEBHOOK_TOKEN", required=False)
            if secret and not hmac.compare_digest(request.query_params.get("token", ""), secret):
                raise HTTPException(403, "token inválido")
            bg.add_task(self.process, agent, payload)
            return {"ok": True}

        return r


# ======================= WhatsApp Cloud API (Meta) =======================
class CloudAPIChannel(_WABase):
    name = "whatsapp_cloud"

    def __init__(self, cfg=None, http: httpx.Client | None = None):
        super().__init__(cfg, http)
        self.caps = Capabilities(buttons=3, lists=10, files=True)

    def to_inbounds(self, payload: dict) -> list[InMsg]:
        out = []
        for entry in payload.get("entry") or []:
            for ch in entry.get("changes") or []:
                value = ch.get("value") or {}
                names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []}
                for m in value.get("messages") or []:  # `statuses` (entregue/lido) são ignorados
                    msg = self._one(m, names)
                    if msg:
                        out.append(msg)
        return out

    def to_inbound(self, payload: dict) -> InMsg | None:
        msgs = self.to_inbounds(payload)
        return msgs[0] if msgs else None

    def _one(self, m: dict, names: dict) -> InMsg | None:
        typ = m.get("type")
        text, choice = None, None
        if typ == "text":
            text = (m.get("text") or {}).get("body")
        elif typ == "interactive":
            it = m.get("interactive") or {}
            rep = it.get("button_reply") or it.get("list_reply") or {}
            choice = rep.get("id")
            text = rep.get("title") or choice
        elif typ == "button":
            b = m.get("button") or {}
            choice = b.get("payload")
            text = b.get("text") or choice
        elif typ in ("image", "video", "document"):
            text = (m.get(typ) or {}).get("caption")
        if not text:
            return None
        frm = str(m.get("from") or "")
        meta: dict[str, Any] = {"phone": "+" + _digits(frm)} if _digits(frm) else {}
        if names.get(frm):
            meta["name"] = names[frm]
        return InMsg(self.name, frm, text, msg_id=m.get("id"), choice_id=choice, meta=meta)

    def to_payloads(self, user_id: str, out: OutMsg) -> list[dict]:
        text, choices, mode = render_text(out, self.caps)
        base = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": user_id}
        if mode == "buttons" and any(len(c["label"]) > 20 for c in choices):
            mode = "list"  # rótulo não cabe no botão → lista (título 24 + descrição)
        payloads: list[dict] = []
        if mode in ("buttons", "list"):
            body = text
            if len(text) > 1024:  # limite do corpo interativo
                payloads += [{**base, "type": "text", "text": {"body": t}} for t in split_text(text, MAX_TEXT)]
                body = "Escolha uma opção:"
            if mode == "buttons":
                action = {"buttons": [{"type": "reply", "reply": {"id": str(c["id"])[:256], "title": c["label"][:20]}} for c in choices]}
                inter = {"type": "button", "body": {"text": body}, "action": action}
            else:
                rows = []
                for c in choices:
                    row = {"id": str(c["id"])[:200], "title": c["label"][:24]}
                    if len(c["label"]) > 24:
                        row["description"] = c["label"][24:96]
                    rows.append(row)
                inter = {"type": "list", "body": {"text": body}, "action": {"button": "Ver opções", "sections": [{"title": "Opções", "rows": rows}]}}
            payloads.append({**base, "type": "interactive", "interactive": inter})
        else:
            payloads += [{**base, "type": "text", "text": {"body": t}} for t in split_text(text, MAX_TEXT)]
        for a in out.attachments:
            if a.get("url"):
                payloads.append({**base, "type": "document", "document": {"link": a["url"], "filename": a.get("name", "arquivo")}})
        return payloads

    def payload_text(self, payload: dict) -> str:
        t = payload.get("type")
        if t == "text":
            return payload["text"]["body"]
        if t == "document":
            return f"[arquivo] {payload['document']['link']}"
        inter = payload["interactive"]
        if inter["type"] == "button":
            labels = [b["reply"]["title"] for b in inter["action"]["buttons"]]
        else:
            labels = [r["title"] + r.get("description", "") for s in inter["action"]["sections"] for r in s["rows"]]
        return inter["body"]["text"] + "\n" + _labels_line(labels)

    def _post(self, payload: dict) -> None:
        graph = str(self._conf.get("graph_base") or "https://graph.facebook.com/v20.0").rstrip("/")
        pid = self._conf.get("phone_number_id") or ""
        if not pid:
            raise RuntimeError("configure channels.whatsapp_cloud.phone_number_id")
        r = self.http.post(f"{graph}/{pid}/messages", json=payload, headers={"Authorization": f"Bearer {self._env('token_env', 'WHATSAPP_TOKEN')}"})
        if r.status_code >= 400:
            raise RuntimeError(f"Cloud API falhou: HTTP {r.status_code} {r.text[:200]}")

    @property
    def _conf(self) -> dict:
        # Cloud API pode ter seção própria (`channels.whatsapp_cloud`, se o config tiver) ou usar `channels.whatsapp`
        if self.cfg is None:
            return {}
        own = getattr(self.cfg.channels, "whatsapp_cloud", None)
        return dict(own) if own else dict(self.cfg.channels.whatsapp)

    def verify_signature(self, body: bytes, header: str | None) -> bool:
        secret = self._env("app_secret_env", "WHATSAPP_APP_SECRET", required=False)
        if not secret:
            return True  # sem app secret configurado não há como validar
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return bool(header) and hmac.compare_digest(expected, header)

    def native_inbound(self, user_id, text, meta=None, choice=None, msg_id=None):
        mid = msg_id or f"wamid.{uuid.uuid4().hex}"
        if choice:
            m = {"from": user_id, "id": mid, "type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": choice, "title": text}}}
        else:
            m = {"from": user_id, "id": mid, "type": "text", "text": {"body": text}}
        value = {"messaging_product": "whatsapp", "contacts": [{"wa_id": user_id, "profile": {"name": (meta or {}).get("name", "")}}], "messages": [m]}
        return {"object": "whatsapp_business_account", "entry": [{"id": "0", "changes": [{"field": "messages", "value": value}]}]}

    def router(self, agent):
        r = APIRouter()

        @r.get("/webhook/whatsapp-cloud")
        def verify(request: Request):
            q = request.query_params
            token = self._conf.get("verify_token") or self._env("verify_token_env", "WHATSAPP_VERIFY_TOKEN", required=False)
            if q.get("hub.mode") == "subscribe" and token and hmac.compare_digest(q.get("hub.verify_token", ""), str(token)):
                return PlainTextResponse(q.get("hub.challenge", ""))
            raise HTTPException(403, "verify_token inválido")

        @r.post("/webhook/whatsapp-cloud")
        async def receive(request: Request, bg: BackgroundTasks):
            body = await request.body()
            if not self.verify_signature(body, request.headers.get("x-hub-signature-256")):
                raise HTTPException(403, "assinatura inválida")
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                raise HTTPException(400, "JSON inválido")
            bg.add_task(self.process, agent, payload)  # roda em thread: agent.handle é bloqueante
            return {"ok": True}

        return r
