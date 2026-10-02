"""Extração de texto principal de HTML e de documentos locais."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urldefrag

from bs4 import BeautifulSoup

DROP_TAGS = ["script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "iframe", "template", "button"]


@dataclass
class Doc:
    url: str
    title: str
    sections: list[tuple[str, str]]  # (heading, text)
    links: list[str] = field(default_factory=list)
    forms: list[dict] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(f"{h}\n{t}" if h else t for h, t in self.sections)


def _clean(s: str) -> str:
    return re.sub(r"[ \t ]+", " ", re.sub(r"\n\s*\n+", "\n", s)).strip()


def summarize_forms(soup: BeautifulSoup, base_url: str) -> list[dict]:
    out = []
    for f in soup.find_all("form"):
        fields = f.find_all(["input", "select", "textarea"])
        visible = [x for x in fields if x.get("type", "").lower() not in ("hidden", "submit", "button", "reset", "image")]
        out.append(
            {
                "action": urljoin(base_url, f.get("action") or base_url),
                "method": (f.get("method") or "get").lower(),
                "fields": len(visible),
                "names": ",".join(x.get("name") or x.get("id") or "?" for x in visible)[:300],
            }
        )
    return out


def html_to_doc(html: str, url: str) -> Doc:
    soup = BeautifulSoup(html, "html.parser")
    title = _clean(soup.title.get_text()) if soup.title else url
    links = []
    for a in soup.find_all("a", href=True):
        href = urldefrag(urljoin(url, a["href"]))[0]
        if href.startswith("http"):
            links.append(href)
    forms = summarize_forms(soup, url)
    for t in soup(DROP_TAGS):
        t.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    sections: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []
    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th", "dd", "dt", "blockquote", "pre", "summary"]):
        if el.find(["p", "li", "h1", "h2", "h3", "h4"]):  # evita duplicar texto de containers
            continue
        txt = _clean(el.get_text(" "))
        if not txt:
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            if buf:
                sections.append((heading, "\n".join(buf)))
                buf = []
            heading = txt[:200]
        else:
            buf.append(txt)
    if buf:
        sections.append((heading, "\n".join(buf)))
    if not sections:  # páginas sem marcação semântica
        txt = _clean(root.get_text("\n"))
        if txt:
            sections = [("", txt)]
    return Doc(url=url, title=title, sections=sections, links=links, forms=forms)


def file_to_doc(path: Path) -> Doc:
    suf = path.suffix.lower()
    uri = path.resolve().as_uri()
    if suf in (".html", ".htm"):
        return html_to_doc(path.read_text(encoding="utf-8", errors="ignore"), uri)
    if suf == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        secs = [(f"página {i + 1}", _clean(p.extract_text() or "")) for i, p in enumerate(reader.pages)]
        return Doc(url=uri, title=path.stem, sections=[s for s in secs if s[1]])
    text = path.read_text(encoding="utf-8", errors="ignore")
    if suf == ".md":
        sections, heading, buf = [], "", []
        for line in text.splitlines():
            m = re.match(r"^#{1,4}\s+(.*)", line)
            if m:
                if buf:
                    sections.append((heading, _clean("\n".join(buf))))
                heading, buf = m.group(1).strip(), []
            else:
                buf.append(line)
        if buf:
            sections.append((heading, _clean("\n".join(buf))))
        title = sections[0][0] if sections and sections[0][0] else path.stem
        return Doc(url=uri, title=title, sections=[s for s in sections if s[1]])
    return Doc(url=uri, title=path.stem, sections=[("", _clean(text))])


def chunk_doc(doc: Doc, words: int = 220, overlap: int = 40) -> list[dict]:
    """Divide por seção; seções longas viram janelas com sobreposição."""
    chunks = []
    for heading, text in doc.sections:
        toks = text.split()
        if len(toks) < 8 and not heading:
            continue
        step = max(1, words - overlap)
        for i in range(0, max(1, len(toks)), step):
            piece = " ".join(toks[i : i + words])
            if not piece:
                break
            chunks.append({"url": doc.url, "title": doc.title, "heading": heading, "text": piece})
            if i + words >= len(toks):
                break
    # junta pedaços muito pequenos ao anterior da mesma página
    merged: list[dict] = []
    for c in chunks:
        if merged and len(c["text"].split()) < 25 and len(merged[-1]["text"].split()) < words:
            merged[-1]["text"] += f"\n{c['heading']}: {c['text']}" if c["heading"] else f"\n{c['text']}"
        else:
            merged.append(c)
    return merged
