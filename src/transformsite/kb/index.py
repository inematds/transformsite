"""Índice híbrido em um único arquivo SQLite: FTS5 (BM25) + embeddings (cosseno em numpy)."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages(
  url TEXT PRIMARY KEY, title TEXT, hash TEXT, fetched_at REAL, words INTEGER, forms INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, url TEXT, title TEXT, heading TEXT, text TEXT, emb BLOB);
CREATE INDEX IF NOT EXISTS chunks_url ON chunks(url);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(norm, content='', tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

STOP = set(
    """a o e é de da do das dos que em um uma para por com no na nos nas se os as ao à
    como qual quais quando onde quem eu você vc meu minha tem ter há sobre mais pra pro
    me isso esse essa este esta the of to and is in what how""".split()
)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def terms(q: str) -> list[str]:
    return [t for t in re.findall(r"\w+", norm(q)) if t not in STOP and len(t) > 1]


@dataclass
class Hit:
    id: int
    url: str
    title: str
    heading: str
    text: str
    score: float


class Index:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.executescript(SCHEMA)
        self._mat: np.ndarray | None = None
        self._ids: list[int] = []

    # ---------- escrita ----------
    def page_hash(self, url: str) -> str | None:
        r = self.db.execute("SELECT hash FROM pages WHERE url=?", (url,)).fetchone()
        return r[0] if r else None

    def upsert_page(self, url: str, title: str, text: str, chunks: list[dict], embs, forms: int = 0) -> None:
        h = hashlib.sha256(text.encode()).hexdigest()
        self.delete_page(url)
        self.db.execute(
            "INSERT INTO pages VALUES(?,?,?,?,?,?)", (url, title, h, time.time(), len(text.split()), forms)
        )
        for i, c in enumerate(chunks):
            emb = None
            if embs is not None:
                emb = np.asarray(embs[i], dtype=np.float32).tobytes()
            cur = self.db.execute(
                "INSERT INTO chunks(url,title,heading,text,emb) VALUES(?,?,?,?,?)",
                (url, c["title"], c["heading"], c["text"], emb),
            )
            self.db.execute(
                "INSERT INTO chunks_fts(rowid, norm) VALUES(?,?)",
                (cur.lastrowid, norm(f"{c['title']} {c['heading']} {c['text']}")),
            )
        self.db.commit()
        self._mat = None

    def delete_page(self, url: str) -> None:
        ids = [r[0] for r in self.db.execute("SELECT id FROM chunks WHERE url=?", (url,))]
        for i in ids:
            row = self.db.execute("SELECT title, heading, text FROM chunks WHERE id=?", (i,)).fetchone()
            self.db.execute(
                "INSERT INTO chunks_fts(chunks_fts, rowid, norm) VALUES('delete', ?, ?)",
                (i, norm(f"{row[0]} {row[1]} {row[2]}")),
            )
        self.db.execute("DELETE FROM chunks WHERE url=?", (url,))
        self.db.execute("DELETE FROM pages WHERE url=?", (url,))
        self._mat = None

    def set_meta(self, k: str, v: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))
        self.db.commit()

    def get_meta(self, k: str) -> str | None:
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    # ---------- leitura ----------
    def stats(self) -> dict:
        p = self.db.execute("SELECT count(*), max(fetched_at) FROM pages").fetchone()
        c = self.db.execute("SELECT count(*), sum(emb IS NOT NULL) FROM chunks").fetchone()
        return {"pages": p[0], "last_fetch": p[1], "chunks": c[0], "with_embeddings": c[1] or 0}

    def urls(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT url FROM pages")}

    def get(self, ids: list[int]) -> dict[int, Hit]:
        if not ids:
            return {}
        q = f"SELECT id,url,title,heading,text FROM chunks WHERE id IN ({','.join('?' * len(ids))})"
        return {r[0]: Hit(r[0], r[1], r[2], r[3], r[4], 0.0) for r in self.db.execute(q, ids)}

    def bm25(self, query: str, k: int = 20) -> list[tuple[int, float]]:
        ts = terms(query)
        if not ts:
            return []
        expr = " OR ".join(f'"{t}"' for t in ts)
        try:
            rows = self.db.execute(
                "SELECT rowid, bm25(chunks_fts) FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
                (expr, k),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(r[0], -r[1]) for r in rows]

    def _matrix(self):
        if self._mat is None:
            rows = self.db.execute("SELECT id, emb FROM chunks WHERE emb IS NOT NULL").fetchall()
            if not rows:
                self._mat, self._ids = np.zeros((0, 1), dtype=np.float32), []
            else:
                self._ids = [r[0] for r in rows]
                m = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
                m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
                self._mat = m
        return self._mat, self._ids

    def vector(self, qemb: list[float], k: int = 20) -> list[tuple[int, float]]:
        m, ids = self._matrix()
        if not ids:
            return []
        q = np.asarray(qemb, dtype=np.float32)
        if q.shape[0] != m.shape[1]:
            return []
        q /= np.linalg.norm(q) + 1e-9
        sims = m @ q
        top = np.argsort(-sims)[:k]
        return [(ids[i], float(sims[i])) for i in top]

    def search(self, query: str, qemb: list[float] | None = None, k: int = 6) -> list[Hit]:
        """Fusão por Reciprocal Rank Fusion (BM25 + vetor)."""
        lists = [self.bm25(query, 30)]
        vec = self.vector(qemb, 30) if qemb is not None else []
        if vec:
            lists.append(vec)
        fused: dict[int, float] = {}
        for lst in lists:
            for rank, (cid, _s) in enumerate(lst):
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (60 + rank)
        best = sorted(fused.items(), key=lambda x: -x[1])[: k * 2]
        hits = self.get([c for c, _ in best])
        out, per_url = [], {}
        vsim = dict(vec)
        for cid, s in best:
            h = hits[cid]
            if per_url.get(h.url, 0) >= 2:  # diversidade: no máx. 2 trechos por página
                continue
            per_url[h.url] = per_url.get(h.url, 0) + 1
            h.score = vsim.get(cid, s)
            out.append(h)
            if len(out) >= k:
                break
        return out
