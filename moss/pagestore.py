"""The Seed Vault's community pages: where uploads are kept, and the limits.

    ~/.moss/seed-vault/pages/<author id>/<name>.mosspage   newest version of each page
    ~/.moss/seed-vault/pages/uploads.json                  today's upload counts

Every page is stored exactly as its author signed it, so a reader checks the
author's signature, not the vault's word. A deletion is stored too (a signed
"deleted" version), which is what stops an old copy being uploaded again.

The upload ledger keeps only today's and yesterday's counts per author id;
there's no history of who uploaded what beyond what the limits need.
"""

from __future__ import annotations

import datetime
import json
import os
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
        return page


def _yesterday(day: str) -> str:
    d = datetime.date.fromisoformat(day) - datetime.timedelta(days=1)
    return d.isoformat()
