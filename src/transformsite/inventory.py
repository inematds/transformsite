"""Fase 0 — inventário: páginas e formulários do site, com prioridade sugerida."""

from __future__ import annotations

import csv
from pathlib import Path

from .kb.crawl import Crawler
from .kb.extract import html_to_doc


def inventory(urls: list[str], out: Path, max_pages=200, sitemaps: list[str] | None = None, exclude: list[str] | None = None, crawler: Crawler | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    crawler = crawler or Crawler(exclude=exclude or [], max_pages=max_pages)
    pages, forms = [], []

    def on_page(page):
        if page.status != 200 or not page.body:
            pages.append({"url": page.url, "status": page.status, "title": "", "words": 0, "forms": 0, "links": 0})
            return []
        doc = html_to_doc(page.body, page.url)
        pages.append({"url": page.url, "status": page.status, "title": doc.title, "words": len(doc.text.split()), "forms": len(doc.forms), "links": len(doc.links)})
        for f in doc.forms:
            kind = _classify(f)
            forms.append({"page": page.url, **f, "kind": kind, "priority": _priority(kind, f["fields"])})
        return doc.links

    for _ in crawler.crawl(urls, sitemaps or [], on_page=on_page):
        pass
    with open(out / "paginas.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["url", "status", "title", "words", "forms", "links"])
        w.writeheader()
        w.writerows(pages)
    # dedup de formulários repetidos em todas as páginas (ex.: newsletter no rodapé)
    uniq = {}
    for f in forms:
        key = (f["action"], f["method"], f["names"])
        if key in uniq:
            uniq[key]["pages"] += 1
        else:
            uniq[key] = {**f, "pages": 1}
    rows = sorted(uniq.values(), key=lambda r: (-r["priority"], -r["pages"]))
    with open(out / "formularios.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["priority", "kind", "page", "pages", "action", "method", "fields", "names"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in w.fieldnames})
    return {"pages": len(pages), "forms_unique": len(rows), "out": str(out)}


def _classify(f: dict) -> str:
    n = f["names"].lower()
    if any(k in n for k in ("search", "busca", "q,", "query")) or n in ("q", "s"):
        return "busca"
    if "password" in n or "senha" in n:
        return "login"
    if f["fields"] <= 1 and "email" in n:
        return "newsletter"
    if any(k in n for k in ("data", "date", "horario", "hora")):
        return "agendamento"
    if any(k in n for k in ("mensagem", "message", "assunto", "subject")):
        return "contato"
    return "outro"


def _priority(kind: str, fields: int) -> int:
    base = {"agendamento": 5, "contato": 4, "outro": 3, "newsletter": 2, "login": 1, "busca": 0}[kind]
    return base * 10 + min(fields, 9)
