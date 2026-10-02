"""Estado durável em SQLite (WAL): sessões, mensagens, eventos, tickets, auditoria."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, channel TEXT, user_id TEXT, status TEXT DEFAULT 'bot',
  state TEXT DEFAULT '{}', meta TEXT DEFAULT '{}', created_at REAL, updated_at REAL,
  UNIQUE(channel, user_id));
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, session_id TEXT, direction TEXT, author TEXT, text TEXT,
  data TEXT DEFAULT '{}', ts REAL, dedup TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS messages_dedup ON messages(session_id, dedup) WHERE dedup IS NOT NULL;
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, session_id TEXT, kind TEXT, data TEXT DEFAULT '{}', ts REAL);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, ts);
CREATE TABLE IF NOT EXISTS tickets(
  id TEXT PRIMARY KEY, session_id TEXT, queue TEXT, kind TEXT, subject TEXT, body TEXT,
  status TEXT DEFAULT 'open', data TEXT DEFAULT '{}', created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS actions(
  idem TEXT PRIMARY KEY, tool TEXT, result TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS appointments(
  id TEXT PRIMARY KEY, start TEXT, end TEXT, title TEXT, data TEXT DEFAULT '{}',
  status TEXT DEFAULT 'confirmed', created_at REAL);
"""

PII_PATTERNS = [
    (re.compile(r"\b(\d{3})\.?(\d{3})\.?(\d{3})-?(\d{2})\b"), lambda m: f"***.***.{m.group(3)}-{m.group(4)}"),
    (re.compile(r"\b[\w.+-]+@([\w-]+\.[\w.]+)\b"), lambda m: f"***@{m.group(1)}"),
    (re.compile(r"(\+?55\s?)?\(?\d{2}\)?\s?9?\d{4}[-\s]?(\d{4})\b"), lambda m: f"(**) *****-{m.group(2)}"),
]


def mask_pii(text: str) -> str:
    for pat, rep in PII_PATTERNS:
        text = pat.sub(rep, text)
    return text


class Store:
    def __init__(self, path: Path | str, audit_path: Path | str | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.audit_path = Path(audit_path) if audit_path else self.path.parent / "audit.jsonl"

    # ---------- sessões ----------
    def session(self, channel: str, user_id: str, meta: dict | None = None) -> dict:
        with self.lock:
            r = self.db.execute("SELECT * FROM sessions WHERE channel=? AND user_id=?", (channel, user_id)).fetchone()
            now = time.time()
            if r is None:
                sid = uuid.uuid4().hex[:16]
                self.db.execute(
                    "INSERT INTO sessions(id,channel,user_id,state,meta,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (sid, channel, user_id, "{}", json.dumps(meta or {}), now, now),
                )
                self.db.commit()
                r = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            elif meta:
                m = json.loads(r["meta"])
                m.update({k: v for k, v in meta.items() if v})
                self.db.execute("UPDATE sessions SET meta=? WHERE id=?", (json.dumps(m), r["id"]))
                self.db.commit()
                r = self.db.execute("SELECT * FROM sessions WHERE id=?", (r["id"],)).fetchone()
            return self._row(r)

    def get_session(self, sid: str) -> dict | None:
        r = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return self._row(r) if r else None

    def _row(self, r) -> dict:
        d = dict(r)
        d["state"] = json.loads(d["state"] or "{}")
        d["meta"] = json.loads(d["meta"] or "{}")
        return d

    def save_state(self, sid: str, state: dict) -> None:
        with self.lock:
            self.db.execute("UPDATE sessions SET state=?, updated_at=? WHERE id=?", (json.dumps(state), time.time(), sid))
            self.db.commit()

    def set_status(self, sid: str, status: str) -> None:
        with self.lock:
            self.db.execute("UPDATE sessions SET status=?, updated_at=? WHERE id=?", (status, time.time(), sid))
            self.db.commit()

    def list_sessions(self, limit=100, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM sessions"
        args: list[Any] = []
        if status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        return [self._row(r) for r in self.db.execute(q, args)]

    # ---------- mensagens ----------
    def add_message(self, sid: str, direction: str, text: str, author="", data: dict | None = None, dedup=None) -> bool:
        """Grava mensagem (texto com PII mascarada). Retorna False se `dedup` já existe (mensagem duplicada)."""
        with self.lock:
            try:
                self.db.execute(
                    "INSERT INTO messages(session_id,direction,author,text,data,ts,dedup) VALUES(?,?,?,?,?,?,?)",
                    (sid, direction, author, mask_pii(text or ""), json.dumps(data or {}), time.time(), dedup),
                )
                self.db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def messages(self, sid: str, limit=50) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?", (sid, limit)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ---------- eventos / métricas ----------
    def event(self, sid: str | None, kind: str, **data) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO events(session_id,kind,data,ts) VALUES(?,?,?,?)", (sid, kind, json.dumps(data), time.time())
            )
            self.db.commit()

    def events(self, since: float = 0, kind: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM events WHERE ts>=?", [since]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        return [dict(r) | {"data": json.loads(r["data"])} for r in self.db.execute(q + " ORDER BY id", args)]

    # ---------- tickets / handoff ----------
    def ticket(self, sid: str | None, queue: str, kind: str, subject: str, body: str, data: dict | None = None) -> str:
        tid = "T" + time.strftime("%y%m%d") + "-" + uuid.uuid4().hex[:5].upper()
        now = time.time()
        with self.lock:
            self.db.execute(
                "INSERT INTO tickets VALUES(?,?,?,?,?,?,?,?,?,?)",
                (tid, sid, queue, kind, subject, body, "open", json.dumps(data or {}), now, now),
            )
            self.db.commit()
        return tid

    def tickets(self, status: str | None = None, limit=200) -> list[dict]:
        q, args = "SELECT * FROM tickets", []
        if status:
            q += " WHERE status=?"
            args.append(status)
        return [dict(r) for r in self.db.execute(q + " ORDER BY created_at DESC LIMIT ?", [*args, limit])]

    def close_ticket(self, tid: str) -> None:
        with self.lock:
            self.db.execute("UPDATE tickets SET status='closed', updated_at=? WHERE id=?", (time.time(), tid))
            self.db.commit()

    # ---------- idempotência ----------
    def action_done(self, idem: str) -> dict | None:
        r = self.db.execute("SELECT result FROM actions WHERE idem=?", (idem,)).fetchone()
        return json.loads(r[0]) if r else None

    def action_save(self, idem: str, tool: str, result: dict) -> None:
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO actions VALUES(?,?,?,?)", (idem, tool, json.dumps(result), time.time()))
            self.db.commit()

    # ---------- auditoria ----------
    def audit(self, **rec) -> None:
        rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        line = mask_pii(json.dumps(rec, ensure_ascii=False, default=str))
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ---------- privacidade ----------
    def forget(self, channel: str, user_id: str) -> int:
        with self.lock:
            r = self.db.execute("SELECT id FROM sessions WHERE channel=? AND user_id=?", (channel, user_id)).fetchone()
            if not r:
                return 0
            sid = r[0]
            n = self.db.execute("DELETE FROM messages WHERE session_id=?", (sid,)).rowcount
            self.db.execute("UPDATE sessions SET state='{}', meta='{}', user_id=? WHERE id=?", (f"forgotten:{sid}", sid))
            self.db.commit()
            return n

    def purge(self, transcripts_days: int) -> int:
        cut = time.time() - transcripts_days * 86400
        with self.lock:
            n = self.db.execute("DELETE FROM messages WHERE ts<?", (cut,)).rowcount
            self.db.commit()
            return n
