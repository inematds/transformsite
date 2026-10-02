"""Canal e-mail.

- Entrada: dict {from, subject, body, message_id, in_reply_to} (vindo do IMAP ou de testes).
  Remove o texto citado (">"), a assinatura ("-- ") e o histórico ("Em ... escreveu:" / "On ... wrote:").
- Saída: e-mail "Re: <assunto>" com as opções numeradas (sem botões). Várias respostas de um turno
  podem ser juntas num único e-mail (`send_all`) — e-mail tem cadência de thread, não de chat.
- Envio via `transformsite.mailer.send_email` (SMTP se configurado; senão .eml em data/outbox).
- Recebimento: `run_poller` lê UNSEEN por IMAP a cada 60s e marca como lido.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import os
import re
import uuid
from email.utils import parseaddr

from ..messages import Capabilities, InMsg, OutMsg, render_text
from . import Channel

log = logging.getLogger(__name__)

QUOTE_HEADER = re.compile(r"^\s*(em|on)\b.{0,300}\b(escreveu|wrote)\s*:\s*$", re.I | re.S)
OUTLOOK_SEP = re.compile(r"^\s*(-{2,}\s*(mensagem original|original message)\s*-{2,}|_{10,})\s*$", re.I)
HEADER_BLOCK = re.compile(r"^\s*(de|from)\s*:\s*.+", re.I)
RE_PREFIX = re.compile(r"^\s*((re|res|fw|fwd|enc|rif|aw)\s*:\s*)+", re.I)


def normalize_address(addr: str) -> str:
    _, a = parseaddr(addr or "")
    return (a or addr or "").strip().lower()


def clean_body(body: str) -> str:
    """Só o texto novo do cliente: corta histórico, citação e assinatura."""
    lines = (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        if line.rstrip() in ("--", "-- ") or line == "-- ":
            break
        if OUTLOOK_SEP.match(line):
            break
        if QUOTE_HEADER.match(line):
            break
        # cabeçalho de citação quebrado em duas linhas ("Em ter., 1 de out., Fulano <x@y>\nescreveu:")
        if i + 1 < len(lines) and QUOTE_HEADER.match(line + " " + lines[i + 1]) and re.match(r"^\s*(em|on)\b", line, re.I):
            break
        if HEADER_BLOCK.match(line) and out and not out[-1].strip() and i + 1 < len(lines) and re.match(r"^\s*(enviad[oa]|sent|data|date)\s*:", lines[i + 1], re.I):
            break
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip()


def is_auto_mail(headers: dict, own_address: str = "") -> bool:
    """Proteção contra laço: respostas automáticas, listas, bounces e o próprio remetente."""
    h = {k.lower(): str(v).lower() for k, v in (headers or {}).items()}
    if h.get("auto-submitted", "no") not in ("", "no"):
        return True
    if h.get("precedence", "") in ("bulk", "list", "junk", "auto_reply"):
        return True
    if "x-autoreply" in h or "x-autorespond" in h or "list-id" in h:
        return True
    frm = normalize_address(h.get("from", ""))
    if frm.startswith(("mailer-daemon", "postmaster", "no-reply", "noreply", "nao-responda", "naoresponda")):
        return True
    return bool(own_address) and frm == normalize_address(own_address)


def parse_rfc822(raw: bytes) -> dict:
    """Bytes RFC822 → dict do canal (prefere text/plain; cai para HTML sem tags)."""
    m = email.message_from_bytes(raw, policy=email.policy.default)
    body = ""
    part = m.get_body(preferencelist=("plain", "html"))
    if part is not None:
        body = part.get_content()
        if part.get_content_type() == "text/html":
            body = re.sub(r"<br\s*/?>|</p>", "\n", body, flags=re.I)
            body = re.sub(r"<[^>]+>", "", body)
    return {
        "from": str(m.get("From", "")),
        "subject": str(m.get("Subject", "")),
        "body": body,
        "message_id": str(m.get("Message-ID", "")).strip() or None,
        "in_reply_to": str(m.get("In-Reply-To", "")).strip() or None,
        "headers": {k: str(v) for k, v in m.items() if k.lower() in ("auto-submitted", "precedence", "x-autoreply", "x-autorespond", "list-id", "from")},
    }


class EmailChannel(Channel):
    name = "email"

    def __init__(self, cfg=None, imap_factory=None):
        self.cfg = cfg
        self.caps = Capabilities(buttons=0, lists=0)
        self._imap_factory = imap_factory
        self._threads: dict[str, dict] = {}  # user → {subject, message_id} da última mensagem recebida

    # ---------- entrada ----------
    def to_inbound(self, payload: dict) -> InMsg | None:
        addr = normalize_address(payload.get("from", ""))
        if not addr:
            return None
        subject = RE_PREFIX.sub("", payload.get("subject") or "").strip()
        text = clean_body(payload.get("body") or "")
        if not text:
            text = subject  # e-mail só com assunto
        if not text:
            return None
        meta = {"subject": subject, "message_id": payload.get("message_id")}
        if "@" in addr:
            meta["email"] = addr
        name = parseaddr(payload.get("from", ""))[0]
        if name:
            meta["name"] = name
        self._threads[addr] = {"subject": subject, "message_id": payload.get("message_id")}
        return InMsg(self.name, addr, text, msg_id=payload.get("message_id"), meta={k: v for k, v in meta.items() if v})

    # ---------- saída ----------
    def _thread(self, user_id: str, meta: dict | None = None) -> tuple[str, str | None]:
        t = self._threads.get(user_id) or meta or {}
        subj = t.get("subject") or (self.cfg.name if self.cfg is not None else "Atendimento")
        return f"Re: {subj}", t.get("message_id")

    def to_payloads(self, user_id: str, out: OutMsg, meta: dict | None = None) -> list[dict]:
        text, _, _ = render_text(out, self.caps)
        subject, in_reply_to = self._thread(user_id, meta)
        return [{"to": user_id, "subject": subject, "body": text, "in_reply_to": in_reply_to}]

    def payload_text(self, payload: dict) -> str:
        return payload["body"]

    def merge(self, outs: list[OutMsg]) -> OutMsg | None:
        """Junta as respostas de um turno num só e-mail (opções só da última que as tiver)."""
        if not outs:
            return None
        *prev, last = outs
        head = [render_text(o, self.caps)[0] for o in prev]
        text = "\n\n".join(head + [last.text])
        return OutMsg(text, choices=last.choices, citations=last.citations, attachments=[a for o in outs for a in o.attachments], kind=last.kind)

    def send(self, user_id: str, out: OutMsg, meta: dict | None = None) -> None:
        from ..mailer import send_email

        if self.cfg is None:
            raise RuntimeError("EmailChannel.send precisa de cfg")
        for p in self.to_payloads(user_id, out, meta):
            send_email(self.cfg, p["to"], p["subject"], p["body"], in_reply_to=p.get("in_reply_to"))

    def send_all(self, user_id: str, outs: list[OutMsg]) -> None:
        m = self.merge(outs)
        if m:
            self.send(user_id, m)

    def native_inbound(self, user_id, text, meta=None, choice=None, msg_id=None):
        return {
            "from": user_id,
            "subject": "Atendimento",
            "body": f"{text}\n\n-- \nEnviado do meu celular\n\nEm seg., 5 de out. de 2026 às 10:00, Bot <bot@x> escreveu:\n> mensagem anterior",
            "message_id": msg_id or f"<{uuid.uuid4().hex}@sim>",
            "in_reply_to": None,
        }

    # ---------- servidor ----------
    def attach(self, agent) -> None:
        def hook(sess: dict, out: OutMsg) -> None:
            if sess and sess.get("channel") == self.name:
                self.send(sess["user_id"], out, meta=sess.get("meta"))

        agent.outbound_hooks.append(hook)

    def process(self, agent, mail: dict) -> None:
        own = (self.cfg.email.smtp_from or self.cfg.email.imap_user) if self.cfg is not None else ""
        if is_auto_mail(mail.get("headers") or {"from": mail.get("from", "")}, own):
            log.info("e-mail automático ignorado: %s", mail.get("from"))
            return
        msg = self.to_inbound(mail)
        if msg is None:
            return
        self.send_all(msg.user_id, agent.handle(msg))

    def _connect(self):
        if self._imap_factory:
            return self._imap_factory()
        ec = self.cfg.email
        pwd = os.environ.get(ec.imap_password_env, "")
        if not ec.imap_host or not pwd:
            raise RuntimeError(f"IMAP não configurado (email.imap_host e variável {ec.imap_password_env})")
        imap = imaplib.IMAP4_SSL(ec.imap_host, ec.imap_port)
        imap.login(ec.imap_user or ec.smtp_from, pwd)
        return imap

    def poll_once(self, agent) -> int:
        imap = self._connect()
        n = 0
        try:
            imap.select("INBOX")
            typ, data = imap.search(None, "UNSEEN")
            for num in (data[0].split() if data and data[0] else []):
                typ, parts = imap.fetch(num, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
                imap.store(num, "+FLAGS", "\\Seen")  # marca antes: e-mail com defeito não vira laço
                if not raw:
                    continue
                try:
                    self.process(agent, parse_rfc822(raw))
                    n += 1
                except Exception as e:
                    log.exception("e-mail %s: %s", num, e)
        finally:
            try:
                imap.logout()
            except Exception:
                pass
        return n

    def run_poller(self, agent, stop_event, interval: float | None = None) -> None:
        interval = float(interval or (self.cfg.channels.email.get("interval", 60) if self.cfg is not None else 60))
        backoff = interval
        while not stop_event.is_set():
            try:
                self.poll_once(agent)
                backoff = interval
            except Exception as e:
                log.warning("IMAP: %s (nova tentativa em %.0fs)", e, backoff)
                backoff = min(backoff * 2, 900)
            stop_event.wait(backoff)
