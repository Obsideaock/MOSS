"""Where a gateway gets its articles.

WikipediaSource  live, uses Wikipedia's API (reuses fetch_page.py's cleanup)
LocalSource      a folder of .md files, searchable, for offline demos and tests

Both are async and raise SourceError with a message short enough to go
over the air.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from .protocol import SearchResult

# fetch_page.py is a standalone script in tools/, next to this package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import fetch_page  # noqa: E402


class SourceError(Exception):
    pass


CITATION = re.compile(r"\[(?:\d+|[a-z])\]")


def prepare_article(markdown: str, max_bytes: int | None) -> str:
    """Trim what costs airtime but adds nothing to reading."""
    text = CITATION.sub("", markdown)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    if max_bytes and len(text.encode()) > max_bytes:
        cut = text.encode()[:max_bytes].decode(errors="ignore")
        cut = cut.rsplit("\n\n", 1)[0]
        text = cut + "\n\n*(shortened to save airtime)*\n"
    return text


# ---------------------------------------------------------------- Wikipedia

class WikipediaSource:
    def __init__(self, lang: str = "en", max_bytes: int | None = 12000,
                 results: int = 8):
        self.lang, self.max_bytes, self.results = lang, max_bytes, results

    def _get(self, params: dict) -> dict:
        url = (fetch_page.API.format(lang=self.lang) + "?"
               + urllib.parse.urlencode(params))
        req = urllib.request.Request(url, headers={"User-Agent": fetch_page.USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception as exc:
            raise SourceError(f"wikipedia unreachable: {type(exc).__name__}") from exc

    def _search(self, query: str) -> list[SearchResult]:
        data = self._get({
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": query, "gsrlimit": str(self.results),
            "prop": "description",
        })
        pages = sorted(data.get("query", {}).get("pages", {}).values(),
                       key=lambda p: p.get("index", 0))
        return [SearchResult(p["title"], p.get("description", "")[:90])
                for p in pages]

    def _article(self, title: str) -> tuple[str, str]:
        try:
            page = fetch_page.fetch_extract(title.replace(" ", "_"), self.lang)
        except SystemExit as exc:                 # fetch_page's "no such article"
            raise SourceError(str(exc) or "no such article") from exc
        except Exception as exc:
            raise SourceError(f"wikipedia unreachable: {type(exc).__name__}") from exc
        body = page.get("extract", "")
        if not body.strip():
            raise SourceError("article is empty")
        real = page.get("title", title)
        md = fetch_page.to_markdown(real, body, self.lang, drop_refs=True)
        return real, prepare_article(md, self.max_bytes)

    async def search(self, query: str) -> list[SearchResult]:
        return await asyncio.to_thread(self._search, query)

    async def article(self, title: str) -> tuple[str, str]:
        return await asyncio.to_thread(self._article, title)


# ---------------------------------------------------------------- local folder

class LocalSource:
    """Markdown files in a folder. Title is the first '# ' line."""

    def __init__(self, folder: str | Path, max_bytes: int | None = 12000):
        self.folder, self.max_bytes = Path(folder), max_bytes

    def _load(self) -> dict[str, str]:
        docs: dict[str, str] = {}
        for path in sorted(self.folder.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^#\s+(.+)$", text, re.M)
            docs.setdefault(m.group(1).strip() if m else path.stem, text)
        return docs

    @staticmethod
    def _blurb(text: str) -> str:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "*", "|")):
                return line[:90]
        return ""

    async def search(self, query: str) -> list[SearchResult]:
        terms = [t for t in re.findall(r"\w+", query.lower()) if t]
        if not terms:
            return []
        scored = []
        for title, text in self._load().items():
            low, tl = text.lower(), title.lower()
            score = sum(10 * (t in tl) + min(low.count(t), 5) for t in terms)
            if score:
                scored.append((score, title, text))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [SearchResult(t, self._blurb(x)) for _, t, x in scored[:8]]

    async def article(self, title: str) -> tuple[str, str]:
        docs = self._load()
        match = next((t for t in docs if t.lower() == title.strip().lower()), None)
        if match is None:
            raise SourceError(f"no such article: {title[:60]}")
        return match, prepare_article(docs[match], self.max_bytes)
