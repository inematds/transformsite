"""Canal web: widget de chat para embutir em qualquer site.

    <script src="https://SEU-SERVIDOR/widget.js" defer></script>

- `POST /api/web/message` {session, text, choice, id?} → {session, messages: [{text, choices, citations, kind}]}
- `GET  /api/web/poll?session=...` → respostas humanas (painel) pendentes para essa sessão
- `GET  /widget.js` → JS vanilla autocontido (botão flutuante + painel; nunca usa innerHTML com texto do bot)
- `GET  /chat` → página demo com o widget
CORS: `channels.web.allowed_origins` (lista; padrão ["*"]).
"""

from __future__ import annotations

import re
import threading
import uuid
from collections import deque
from copy import copy

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ..messages import Capabilities, InMsg, OutMsg, render_text
from . import Channel

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_IN = 4000


class WebChannel(Channel):
    name = "web"

    def __init__(self, cfg=None):
        self.cfg = cfg
        self.caps = Capabilities(buttons=6, lists=0, markdown=False, files=True)
        self._pending: dict[str, deque] = {}
        self._lock = threading.Lock()

    @property
    def _conf(self) -> dict:
        return dict(self.cfg.channels.web) if self.cfg is not None else {}

    # ---------- entrada ----------
    def to_inbound(self, payload: dict) -> InMsg | None:
        sess = str(payload.get("session") or "")
        text = str(payload.get("text") or "")[:MAX_IN].strip()
        choice = payload.get("choice") or None
        if not sess or not (text or choice):
            return None
        return InMsg(self.name, sess, text or str(choice), msg_id=payload.get("id") or None, choice_id=choice, meta=dict(payload.get("meta") or {}))

    # ---------- saída ----------
    def to_payloads(self, user_id: str, out: OutMsg) -> list[dict]:
        bare = copy(out)
        bare.citations = []  # o widget mostra as fontes como links, fora do texto
        text, choices, _ = render_text(bare, self.caps)
        cites = [{"n": c.get("n"), "url": c.get("url"), "title": c.get("title", "")} for c in out.citations]
        return [{"text": text, "choices": [{"id": str(c["id"]), "label": c["label"]} for c in choices], "citations": cites, "kind": out.kind, "attachments": out.attachments}]

    def payload_text(self, payload: dict) -> str:
        text = payload["text"]
        if payload.get("choices"):
            text += "\n" + " ".join(f"[{c['label']}]" for c in payload["choices"])
        if payload.get("citations"):
            text += "\n\nFontes:\n" + "\n".join(f"[{c['n']}] {c['url']}" for c in payload["citations"])
        return text

    def send(self, user_id: str, out: OutMsg) -> None:
        """Sem conexão aberta com o navegador: enfileira; o widget busca em /api/web/poll."""
        with self._lock:
            q = self._pending.setdefault(user_id, deque(maxlen=50))
            q.extend(self.to_payloads(user_id, out))
            if len(self._pending) > 10000:  # sessões abandonadas não crescem para sempre
                self._pending.pop(next(iter(self._pending)))

    def drain(self, user_id: str) -> list[dict]:
        with self._lock:
            q = self._pending.pop(user_id, None)
        return list(q or [])

    def native_inbound(self, user_id, text, meta=None, choice=None, msg_id=None):
        return {"session": user_id, "text": text, "choice": choice, "id": msg_id, "meta": meta or {}}

    def attach(self, agent) -> None:
        def hook(sess: dict, out: OutMsg) -> None:
            if sess and sess.get("channel") == self.name:
                self.send(sess["user_id"], out)

        agent.outbound_hooks.append(hook)

    # ---------- HTTP ----------
    def _cors(self, origin: str | None) -> dict:
        allowed = self._conf.get("allowed_origins") or ["*"]
        if "*" in allowed:
            allow = "*"
        elif origin and origin in allowed:
            allow = origin
        else:
            return {}
        return {"Access-Control-Allow-Origin": allow, "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type", "Vary": "Origin"}

    def router(self, agent):
        r = APIRouter()

        @r.options("/api/web/message")
        @r.options("/api/web/poll")
        def preflight(request: Request):
            return Response(status_code=204, headers=self._cors(request.headers.get("origin")))

        @r.post("/api/web/message")
        def web_message(body: dict, request: Request):  # síncrono: FastAPI roda em threadpool
            sess = str(body.get("session") or "")
            if not SESSION_RE.match(sess):
                sess = uuid.uuid4().hex
            choice = body.get("choice")
            if choice is not None and (not isinstance(choice, str) or len(choice) > 300):
                raise HTTPException(400, "choice inválido")
            msg = self.to_inbound({"session": sess, "text": body.get("text"), "choice": choice, "id": body.get("id")})
            outs = agent.handle(msg) if msg else []
            messages = self.drain(sess) + [p for o in outs for p in self.to_payloads(sess, o)]
            return JSONResponse({"session": sess, "messages": messages}, headers=self._cors(request.headers.get("origin")))

        @r.get("/api/web/poll")
        def web_poll(session: str, request: Request):
            if not SESSION_RE.match(session):
                raise HTTPException(400, "sessão inválida")
            return JSONResponse({"session": session, "messages": self.drain(session)}, headers=self._cors(request.headers.get("origin")))

        @r.get("/widget.js")
        def widget_js():
            return Response(WIDGET_JS, media_type="application/javascript; charset=utf-8", headers={"Cache-Control": "public, max-age=300"})

        @r.get("/chat")
        def chat_page():
            title = (self.cfg.name if self.cfg is not None else "Atendimento").replace("<", "").replace(">", "")
            return HTMLResponse(DEMO_HTML.replace("{{title}}", title))

        return r


DEMO_HTML = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{title}} — chat</title>
<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;color:#222;background:#fafafa}</style>
</head><body>
<h1>{{title}}</h1>
<p>Página de demonstração. Para usar no seu site, inclua:</p>
<pre>&lt;script src="ENDERECO_DO_SERVIDOR/widget.js" defer&gt;&lt;/script&gt;</pre>
<p>O botão de conversa fica no canto inferior direito.</p>
<script src="/widget.js" defer></script>
</body></html>
"""

WIDGET_JS = r"""(function () {
  "use strict";
  if (window.__tsWidget) return; window.__tsWidget = true;
  var script = document.currentScript;
  var base = script && script.src ? script.src.replace(/\/widget\.js(\?.*)?$/, "") : "";
  if (script && script.getAttribute("data-api")) base = script.getAttribute("data-api").replace(/\/$/, "");
  var title = (script && script.getAttribute("data-title")) || "Atendimento";
  var KEY = "ts_chat_session";
  var session = null;
  try { session = window.localStorage.getItem(KEY); } catch (e) {}
  var busy = false, timer = null;

  function el(tag, attrs, text) {
    var n = document.createElement(tag);
    if (attrs) for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = String(text);
    return n;
  }
  function safeUrl(u) { return /^https?:\/\//i.test(String(u || "")) ? String(u) : null; }

  var css = el("style", null,
    ".tsw-btn{position:fixed;right:20px;bottom:20px;width:56px;height:56px;border-radius:50%;border:0;background:#1f6feb;color:#fff;font-size:26px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.25);z-index:2147483000}" +
    ".tsw-panel{position:fixed;right:20px;bottom:88px;width:360px;max-width:calc(100vw - 32px);height:520px;max-height:calc(100vh - 110px);background:#fff;color:#1b1b1b;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.3);display:none;flex-direction:column;overflow:hidden;font:14px/1.45 system-ui,sans-serif;z-index:2147483000}" +
    ".tsw-panel.open{display:flex}.tsw-head{background:#1f6feb;color:#fff;padding:12px 14px;font-weight:600;display:flex;justify-content:space-between}" +
    ".tsw-head button{background:none;border:0;color:#fff;font-size:18px;cursor:pointer}" +
    ".tsw-log{flex:1;overflow-y:auto;padding:12px;background:#f5f7fa}" +
    ".tsw-msg{margin:6px 0;padding:8px 11px;border-radius:10px;max-width:85%;white-space:pre-wrap;word-wrap:break-word}" +
    ".tsw-bot{background:#fff;border:1px solid #e3e6ea}.tsw-user{background:#1f6feb;color:#fff;margin-left:auto}" +
    ".tsw-choices{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 8px}" +
    ".tsw-choices button{border:1px solid #1f6feb;background:#fff;color:#1f6feb;border-radius:16px;padding:5px 11px;cursor:pointer;font:inherit}" +
    ".tsw-choices button:disabled{opacity:.5;cursor:default}" +
    ".tsw-src{font-size:12px;margin:2px 0 8px}.tsw-src a{color:#1f6feb;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" +
    ".tsw-form{display:flex;border-top:1px solid #e3e6ea}.tsw-form input{flex:1;border:0;padding:12px;font:inherit;outline:none}" +
    ".tsw-form button{border:0;background:#1f6feb;color:#fff;padding:0 16px;cursor:pointer;font:inherit}" +
    "@media (prefers-color-scheme: dark){.tsw-panel{background:#1d2127;color:#e8e8e8}.tsw-log{background:#15181c}.tsw-bot{background:#23282f;border-color:#333}.tsw-form input{background:#1d2127;color:#e8e8e8}.tsw-choices button{background:#1d2127}}");
  document.head.appendChild(css);

  var btn = el("button", {"class": "tsw-btn", "type": "button", "aria-label": "Abrir conversa"}, "\u{1F4AC}");
  var panel = el("div", {"class": "tsw-panel", "role": "dialog", "aria-label": title});
  var head = el("div", {"class": "tsw-head"});
  head.appendChild(el("span", null, title));
  var close = el("button", {"type": "button", "aria-label": "Fechar"}, "×");
  head.appendChild(close);
  var log = el("div", {"class": "tsw-log", "aria-live": "polite"});
  var form = el("form", {"class": "tsw-form"});
  var input = el("input", {"type": "text", "placeholder": "Escreva sua mensagem…", "maxlength": "2000", "aria-label": "Mensagem"});
  form.appendChild(input);
  form.appendChild(el("button", {"type": "submit"}, "Enviar"));
  panel.appendChild(head); panel.appendChild(log); panel.appendChild(form);
  document.body.appendChild(btn); document.body.appendChild(panel);

  function scroll() { log.scrollTop = log.scrollHeight; }
  function addUser(text) { log.appendChild(el("div", {"class": "tsw-msg tsw-user"}, text)); scroll(); }
  function addBot(m) {
    log.appendChild(el("div", {"class": "tsw-msg tsw-bot"}, m.text || ""));
    if (m.citations && m.citations.length) {
      var src = el("div", {"class": "tsw-src"});
      m.citations.forEach(function (c) {
        var u = safeUrl(c.url); if (!u) return;
        var a = el("a", {"href": u, "target": "_blank", "rel": "noopener noreferrer"}, "[" + c.n + "] " + (c.title || u));
        src.appendChild(a);
      });
      log.appendChild(src);
    }
    if (m.choices && m.choices.length) {
      var box = el("div", {"class": "tsw-choices"});
      m.choices.forEach(function (c) {
        var b = el("button", {"type": "button"}, c.label);
        b.addEventListener("click", function () {
          Array.prototype.forEach.call(box.querySelectorAll("button"), function (x) { x.disabled = true; });
          send(c.label, c.id);
        });
        box.appendChild(b);
      });
      log.appendChild(box);
    }
    scroll();
  }
  function remember(s) { if (!s) return; session = s; try { window.localStorage.setItem(KEY, s); } catch (e) {} }
  function rid() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 10); }

  function send(text, choice) {
    if (busy) return; busy = true;
    addUser(text);
    fetch(base + "/api/web/message", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({session: session, text: text, choice: choice || null, id: rid()})
    }).then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (d) { remember(d.session); (d.messages || []).forEach(addBot); })
      .catch(function () { addBot({text: "Não consegui enviar agora. Tente de novo em instantes."}); })
      .then(function () { busy = false; input.focus(); });
  }
  function poll() {
    if (!session || !panel.classList.contains("open")) return;
    fetch(base + "/api/web/poll?session=" + encodeURIComponent(session))
      .then(function (r) { return r.ok ? r.json() : {messages: []}; })
      .then(function (d) { (d.messages || []).forEach(addBot); })
      .catch(function () {});
  }
  function toggle(open) {
    panel.classList.toggle("open", open);
    if (open) {
      if (!log.childNodes.length) send("oi");
      input.focus();
      if (!timer) timer = setInterval(poll, 5000);
    } else if (timer) { clearInterval(timer); timer = null; }
  }
  btn.addEventListener("click", function () { toggle(!panel.classList.contains("open")); });
  close.addEventListener("click", function () { toggle(false); });
  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var t = input.value.trim(); if (!t) return;
    input.value = ""; send(t, null);
  });
})();
"""
