"""The Seed Vault's community pages: where uploads are kept, and the limits.

    ~/.moss/seed-vault/pages/<author id>/<name>.mosspage   newest version of each page
    ~/.moss/seed-vault/pages/uploads.json                  today's upload counts

Every page is stored exactly as its author signed it, so a reader checks the
author's signature, not the vault's word. A deletion is stored too (a signed
"deleted" version), which is what stops an old copy being uploaded again.

The upload ledger keeps only today's and yesterday's counts per author id;
there's no history of who uploaded what beyond what the limits need.

Search (public pages only; unlisted ones are never indexed or counted):
    score = 10 x text match + 3 x link score + log(1 + downloads) + 1 if recent
The link score works like Google's original PageRank, made hard to game:
links count once per distinct *other* author, and each linking author
weighs more the more other authors link to them. Downloads are a plain
count per public page (no who, no when).
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import pages as PG
from .seal import upload_spores


@dataclass
class Limits:
    cap_spores: int = 250                   # biggest page accepted
    per_author_per_day: int = 3             # uploads (new pages, updates, deletions)
    spores_per_day: int = 2000              # the whole vault's daily upload budget
    storage_bytes: int = 2 * 1024 ** 3      # all community pages together
    accept_uploads: bool = True


class PageStore:
    def __init__(self, folder: str | Path, limits: Limits | None = None,
                 today: Callable[[], str] | None = None):
        self.folder = Path(folder).expanduser()
        self.limits = limits or Limits()
        self._today = today or (lambda: datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"))
        self.folder.mkdir(parents=True, exist_ok=True)
        self._index: list[dict] | None = None

    # ------------------------------------------------------------ reading
    def _path(self, address: str) -> Path:
        aid, name = PG.parse_address(address)
        return self.folder / aid / f"{name}.mosspage"

    def get(self, address: str) -> bytes | None:
        try:
            return self._path(address).read_bytes()
        except (OSError, PG.PageError):
            return None

    def get_page(self, address: str) -> PG.Page | None:
        data = self.get(address)
        if data is None:
            return None
        try:
            return PG.load(data)
        except PG.PageError:
            return None                      # damaged on disk: treat as absent

    def all(self) -> list[PG.Page]:
        out = []
        for f in sorted(self.folder.glob("*/*.mosspage")):
            try:
                out.append(PG.load(f.read_bytes()))
            except (OSError, PG.PageError):
                continue
        return out

    def used_bytes(self) -> int:
        return sum(f.stat().st_size for f in self.folder.glob("*/*.mosspage"))

    # ------------------------------------------------------------ the ledger
    def _ledger_path(self) -> Path:
        return self.folder / "uploads.json"

    def _ledger(self) -> list[dict]:
        try:
            rows = json.loads(self._ledger_path().read_text(encoding="utf-8"))
            return rows if isinstance(rows, list) else []
        except (OSError, ValueError):
            return []

    def _record(self, author: str, spores: int) -> None:
        today = self._today()
        rows = [r for r in self._ledger() if r.get("day", "") >= _yesterday(today)]
        rows.append({"day": today, "author": author, "spores": spores})
        tmp = self._ledger_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(rows), encoding="utf-8")
        os.replace(tmp, self._ledger_path())

    def today_counts(self, author: str | None = None) -> tuple[int, int]:
        """(uploads by this author today, spores uploaded to this vault today)"""
        today = self._today()
        rows = [r for r in self._ledger() if r.get("day") == today]
        return (sum(1 for r in rows if r.get("author") == author),
                sum(int(r.get("spores", 0)) for r in rows))

    # ------------------------------------------------------------ uploads
    def check_offer(self, address: str, version: int, size: int) -> str | None:
        """Why this upload can't be taken, or None if it can. Checked when
        the offer arrives (so a refusal costs no airtime) and again on arrival."""
        lim = self.limits
        if not lim.accept_uploads:
            return "this Seed Vault isn't taking uploads"
        try:
            aid, _ = PG.parse_address(address)
        except PG.PageError:
            return "that isn't a page address"
        if version < 1 or size < 1:
            return "bad offer"
        spores = upload_spores(size)
        if spores > lim.cap_spores:
            return f"too big: {spores} spores, this Seed Vault's cap is {lim.cap_spores}"
        current = self.get_page(address)
        if current is not None and version <= current.version:
            if version == current.version:
                return f"already has v{version} of this page"
            return f"this Seed Vault already has v{current.version}; an older copy can't replace it"
        mine, total = self.today_counts(aid)
        if mine >= lim.per_author_per_day:
            return (f"this author's {lim.per_author_per_day} uploads for today are used up; "
                    f"try again tomorrow")
        if total + spores > lim.spores_per_day:
            return "this Seed Vault's upload airtime for today is used up; try again tomorrow"
        replaced = len(self.get(address) or b"")
        if self.used_bytes() - replaced + size > lim.storage_bytes:
            return "this Seed Vault has no room for more community pages"
        return None

    def store(self, data: bytes, address: str, version: int, size: int) -> PG.Page:
        """Verify and keep an uploaded page. Raises PageError with the reason."""
        page = PG.load(data)                                  # signature, record, body
        if page.address != address or page.version != version or len(data) != size:
            raise PG.PageError("the page that arrived doesn't match what was offered")
        reason = self.check_offer(address, version, size)
        if reason:
            raise PG.PageError(reason)
        PG.check_newer(self.get_page(address), page)
        path = self._path(address)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self._record(PG.parse_address(address)[0], upload_spores(size))
        self._index = None
        return page

    # ------------------------------------------------------------ counts and room
    def _counts_path(self) -> Path:
        return self.folder / "downloads.json"

    def downloads(self) -> dict:
        try:
            d = json.loads(self._counts_path().read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def count_download(self, address: str) -> None:
        """One more download of a public page. Unlisted pages are never counted."""
        page = self.get_page(address)
        if page is None or page.record["visibility"] != PG.PUBLIC or page.deleted:
            return
        d = self.downloads()
        d[address] = int(d.get(address, 0)) + 1
        tmp = self._counts_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, self._counts_path())
        if self._index is not None:
            for row in self._index:
                if row["address"] == address:
                    row["downloads"] += 1

    def room(self) -> dict:
        """Free space, for vault specs: room left under the cap and on the disk."""
        used = self.used_bytes()
        disk = shutil.disk_usage(self.folder).free
        return {"used_mb": round(used / 1024 ** 2, 1),
                "cap_mb": round(self.limits.storage_bytes / 1024 ** 2),
                "free_mb": round(max(0, min(self.limits.storage_bytes - used, disk)) / 1024 ** 2, 1),
                "disk_free_mb": round(disk / 1024 ** 2)}

    def public_count(self) -> int:
        return len(self._build())

    # ------------------------------------------------------------ search
    def _build(self) -> list[dict]:
        if self._index is not None:
            return self._index
        rows = []
        for page in self.all():
            r = page.record
            if page.deleted or r["visibility"] != PG.PUBLIC or r["lock"] is not None:
                continue
            try:
                text = page.text()
            except PG.PageError:
                continue
            aid = PG.parse_address(page.address)[0]
            links = {m.group(0) for m in LINK.finditer(text)} - {page.address}
            rows.append({"address": page.address, "author_id": aid, "title": r["title"],
                         "author": r["author"], "fingerprint": page.fingerprint, "about": r["about"],
                         "version": r["version"], "time": r["time"], "links": links,
                         "words": _words(" ".join((r["title"], r["about"], r["author"], text))),
                         "title_words": _words(r["title"]), "about_words": _words(r["about"]),
                         "author_words": _words(r["author"])})
        # Link score: distinct other authors linking in, each weighted by how
        # many other authors link to *them*.
        by_address = {row["address"]: row for row in rows}
        linkers: dict[str, set[str]] = {a: set() for a in by_address}
        for row in rows:
            for target in row["links"]:
                if target in by_address and by_address[target]["author_id"] != row["author_id"]:
                    linkers[target].add(row["author_id"])
        author_fans: dict[str, set[str]] = {}
        for address, fans in linkers.items():
            author_fans.setdefault(by_address[address]["author_id"], set()).update(fans)
        weight = {a: 1 + math.log2(1 + len(f)) for a, f in author_fans.items()}
        downloads = self.downloads()
        for row in rows:
            row["link_score"] = sum(weight.get(a, 1.0) for a in linkers[row["address"]])
            row["downloads"] = int(downloads.get(row["address"], 0))
        self._index = rows
        return rows

    def search(self, query: str, offset: int = 0, limit: int = 10,
               now: float | None = None) -> tuple[list[dict], bool]:
        """(results, more?) for public community pages. 'author:<id>' lists one author."""
        rows = self._build()
        now = now or time.time()
        query = query.strip()
        if query.lower().startswith("author:"):
            aid = query.split(":", 1)[1].strip().lower()
            hits = sorted((r for r in rows if r["author_id"] == aid), key=lambda r: -r["time"])
        else:
            terms = [t for t in _words(query) if len(t) > 1]
            if not terms:
                return [], False
            scored = []
            for r in rows:
                text = sum(3 if t in r["title_words"] else 2 if t in r["about_words"]
                           else 2 if t in r["author_words"] else 1 if t in r["words"] else 0
                           for t in terms)
                if text == 0:
                    continue
                score = (10 * text + 3 * r["link_score"] + math.log1p(r["downloads"])
                         + (1 if now - r["time"] < 30 * 86400 else 0))
                scored.append((score, r))
            hits = [r for _, r in sorted(scored, key=lambda sr: (-sr[0], -sr[1]["time"]))]
        page = hits[offset:offset + limit]
        return [{"a": r["address"], "t": r["title"], "n": r["author"], "f": r["fingerprint"],
                 "d": r["about"], "v": r["version"]} for r in page], len(hits) > offset + limit


LINK = re.compile(r"moss:[a-z2-7]{26}/~?[a-z0-9-]{1,48}")


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _yesterday(day: str) -> str:
    d = datetime.date.fromisoformat(day) - datetime.timedelta(days=1)
    return d.isoformat()
