"""`transformsite ingest`: site + arquivos → índice com citações."""

from __future__ import annotations

import glob
from pathlib import Path

from ..config import ProjectConfig
from ..llm import LLM
from .crawl import Crawler
from .extract import Doc, chunk_doc, file_to_doc, html_to_doc
from .index import Index


def ingest_doc(index: Index, llm: LLM, doc: Doc, cfg: ProjectConfig, force=False) -> int:
    text = doc.text
    import hashlib

    if not force and index.page_hash(doc.url) == hashlib.sha256(text.encode()).hexdigest():
        return 0
    chunks = chunk_doc(doc, cfg.kb.chunk_words, cfg.kb.overlap_words)
    if not chunks:
        index.delete_page(doc.url)
        return 0
    embs = llm.embed([f"{c['title']} — {c['heading']}\n{c['text']}" for c in chunks])
    index.upsert_page(doc.url, doc.title, text, chunks, embs, forms=len(doc.forms))
    return len(chunks)


def ingest(cfg: ProjectConfig, urls: list[str] | None = None, max_pages: int | None = None, force=False, log=print) -> dict:
    index = Index(cfg.kb_path)
    llm = LLM(cfg.llm)
    seeds = urls if urls is not None else cfg.kb.urls
    sitemaps = [] if urls is not None else cfg.kb.sitemaps
    changed = pages = 0
    seen_urls: set[str] = set()
    if seeds or sitemaps:
        crawler = Crawler(cfg.kb.include, cfg.kb.exclude, max_pages or cfg.kb.max_pages)

        def on_page(page):
            if page.status != 200 or not page.body:
                return []
            doc = html_to_doc(page.body, page.url)
            on_page.docs[page.url] = doc
            return doc.links

        on_page.docs = {}
        for page in crawler.crawl(seeds, sitemaps, on_page=on_page):
            doc = on_page.docs.pop(page.url, None)
            if doc is None:
                continue
            pages += 1
            seen_urls.add(doc.url)
            n = ingest_doc(index, llm, doc, cfg, force)
            changed += 1 if n else 0
            if pages % 20 == 0:
                log(f"  {pages} páginas…")
    for pattern in cfg.kb.files:
        for f in sorted(glob.glob(str(cfg.root / pattern), recursive=True)):
            doc = file_to_doc(Path(f))
            pages += 1
            seen_urls.add(doc.url)
            changed += 1 if ingest_doc(index, llm, doc, cfg, force) else 0
    st = index.stats()
    index.set_meta("embed_model", f"{llm.embed_provider}:{cfg.llm.embed_model}")
    return {"pages_seen": pages, "pages_changed": changed, **st}
