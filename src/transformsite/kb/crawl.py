"""Crawler simples e educado: sitemap + links do mesmo domínio, respeita robots.txt."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

UA = "transformsite/0.1 (+https://github.com/inematds/transformsite)"
SKIP_EXT = re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|mp4|mp3|wav|zip|gz|css|js|woff2?|ttf|json|xml)(\?|$)", re.I)


@dataclass
class Page:
    url: str
    status: int
    content_type: str
    body: str


def _host(u: str) -> str:
    h = urlparse(u).netloc.lower()
    return h[4:] if h.startswith("www.") else h


class Crawler:
    def __init__(self, include=None, exclude=None, max_pages=300, delay=0.1, client: httpx.Client | None = None):
        self.include = [re.compile(p) for p in (include or [])]
        self.exclude = [re.compile(p) for p in (exclude or [])]
        self.max_pages = max_pages
        self.delay = delay
        self.client = client or httpx.Client(headers={"User-Agent": UA}, timeout=25, follow_redirects=True)
        self._robots: dict[str, RobotFileParser] = {}

    def allowed(self, url: str) -> bool:
        if SKIP_EXT.search(urlparse(url).path):
            return False
        if self.include and not any(p.search(url) for p in self.include):
            return False
        if any(p.search(url) for p in self.exclude):
            return False
        pr = urlparse(url)
        base = f"{pr.scheme}://{pr.netloc}"
        if base not in self._robots:
            rp = RobotFileParser()
            try:
                r = self.client.get(base + "/robots.txt")
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            self._robots[base] = rp
        return self._robots[base].can_fetch(UA, url)

    def sitemap_urls(self, sitemap: str, depth: int = 0) -> list[str]:
        try:
            r = self.client.get(sitemap)
            if r.status_code != 200:
                return []
            root = ET.fromstring(r.content)
        except (httpx.HTTPError, ET.ParseError):
            return []
        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        if root.tag.endswith("sitemapindex") and depth < 2:
            out = []
            for loc in root.findall(".//s:sitemap/s:loc", ns):
                out += self.sitemap_urls(loc.text.strip(), depth + 1)
            return out
        return [loc.text.strip() for loc in root.findall(".//s:url/s:loc", ns) if loc.text]

    def fetch(self, url: str) -> Page | None:
        try:
            r = self.client.get(url)
        except httpx.HTTPError:
            return None
        ct = r.headers.get("content-type", "")
        if "html" not in ct and "pdf" not in ct and "text/plain" not in ct:
            return Page(str(r.url), r.status_code, ct, "")
        body = r.text if "pdf" not in ct else ""
        if self.delay:
            time.sleep(self.delay)
        return Page(str(r.url), r.status_code, ct, body)

    def crawl(self, seeds: list[str], sitemaps: list[str] | None = None, follow_links=True, on_page=None):
        """Gera Page por página. `on_page(page)` pode devolver links para seguir."""
        seen: set[str] = set()
        queue: deque[str] = deque()
        hosts = {_host(s) for s in seeds + (sitemaps or [])}
        for sm in sitemaps or []:
            for u in self.sitemap_urls(sm):
                queue.append(u)
        queue.extend(seeds)
        count = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            while queue and count < self.max_pages:
                batch = []
                while queue and len(batch) < 8 and count + len(batch) < self.max_pages:
                    u = queue.popleft().split("#")[0]
                    if u in seen or _host(u) not in hosts or not self.allowed(u):
                        seen.add(u)
                        continue
                    seen.add(u)
                    batch.append(u)
                for page in pool.map(self.fetch, batch):
                    if page is None:
                        continue
                    count += 1
                    links = on_page(page) if on_page else None
                    yield page
                    if follow_links and links:
                        for link in links:
                            if link not in seen:
                                queue.append(link)
