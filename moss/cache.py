"""A local cache of everything fetched over the air.

    <cache>/index.json             titles, sizes, timestamps
    <cache>/articles/<slug>.md     the article text, exactly as received
    <cache>/searches.json          past searches and their result lists

Airtime is the scarce thing here, so a cached article is shown instantly and
costs nothing. Writes are atomic: a crash mid-write never leaves a half file.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .protocol import SearchResult

DEFAULT_DIR = Path.home() / ".moss" / "cache"


def slugify(title: str) -> str:
    slug = re.sub(r"[^\w\- ]+", "", title, flags=re.UNICODE).strip().replace(" ", "_")
    return slug[:80] or "article"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class CachedArticle:
    title: str
    file: str
    bytes: int
    fetched_at: float


class Cache:
    def __init__(self, folder: str | Path | None = None):
        self.folder = Path(folder) if folder else DEFAULT_DIR
        self.articles = self.folder / "articles"
        self._index_path = self.folder / "index.json"
        self._search_path = self.folder / "searches.json"

    # ------------------------------------------------------------ helpers

    def _read(self, path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _key(text: str) -> str:
        return " ".join(text.lower().split())

    # ------------------------------------------------------------ articles

    def get_article(self, title: str) -> tuple[CachedArticle, str] | None:
        entry = self._read(self._index_path).get(self._key(title))
        if not entry:
            return None
        try:
            text = (self.articles / entry["file"]).read_text(encoding="utf-8")
        except OSError:
            return None
        return CachedArticle(entry["title"], entry["file"], entry["bytes"],
                             entry["fetched_at"]), text

    def put_article(self, title: str, text: str,
                    aliases: list[str] | None = None) -> CachedArticle:
        """Store under the canonical title, plus any other names it was asked
        for by (a redirect, different capitalisation)."""
        index = self._read(self._index_path)
        file = slugify(title) + ".md"
        _atomic_write(self.articles / file, text)
        entry = {"title": title, "file": file, "bytes": len(text.encode()),
                 "fetched_at": time.time()}
        for name in [title, *(aliases or [])]:
            index[self._key(name)] = entry
        _atomic_write(self._index_path, json.dumps(index, indent=1))
        return CachedArticle(**entry)

    def list_articles(self) -> list[CachedArticle]:
        """One row per stored article, even if it has several names."""
        unique = {e["file"]: e for e in self._read(self._index_path).values()}
        entries = [CachedArticle(**e) for e in unique.values()]
        return sorted(entries, key=lambda a: a.fetched_at, reverse=True)

    def forget(self, title: str) -> bool:
        index = self._read(self._index_path)
        entry = index.get(self._key(title))
        if not entry:
            return False
        index = {k: e for k, e in index.items() if e["file"] != entry["file"]}
        (self.articles / entry["file"]).unlink(missing_ok=True)
        _atomic_write(self._index_path, json.dumps(index, indent=1))
        return True

    # ------------------------------------------------------------ searches

    def get_search(self, query: str) -> tuple[list[SearchResult], float] | None:
        entry = self._read(self._search_path).get(self._key(query))
        if not entry:
            return None
        return ([SearchResult(r["title"], r.get("blurb", "")) for r in entry["results"]],
                entry["at"])

    def put_search(self, query: str, results: list[SearchResult]) -> None:
        data = self._read(self._search_path)
        data[self._key(query)] = {
            "query": query, "at": time.time(),
            "results": [{"title": r.title, "blurb": r.blurb} for r in results]}
        _atomic_write(self._search_path, json.dumps(data, indent=1))
