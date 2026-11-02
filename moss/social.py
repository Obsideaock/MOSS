"""Who you follow, what you favourite, what you open: ~/.moss/social.json.

All of it stays on this device. It's only ever used to re-sort search
results locally; nothing here is sent to a Seed Vault, so no vault owner
can see who follows whom.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .config import moss_home

LINK = re.compile(r"moss:[a-z2-7]{26}/~?[a-z0-9-]{1,48}")


class Social:
    def __init__(self, home: Path | None = None):
        self.path = Path(home or moss_home()) / "social.json"
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        self.follows: dict = d.get("follows", {}) if isinstance(d, dict) else {}
        self.favourites: dict = d.get("favourites", {}) if isinstance(d, dict) else {}
        self.opened: dict = d.get("opened", {}) if isinstance(d, dict) else {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"follows": self.follows, "favourites": self.favourites,
                                   "opened": self.opened}, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- following authors (by author id; the name is just for showing)
    def follow(self, author_id: str, name: str = "") -> None:
        self.follows[author_id] = name
        self._save()

    def unfollow(self, author_id: str) -> None:
        self.follows.pop(author_id, None)
        self._save()

    def is_following(self, author_id: str) -> bool:
        return author_id in self.follows

    # -- favourite pages
    def favourite(self, address: str, title: str = "", on: bool = True) -> None:
        if on:
            self.favourites[address] = title
        else:
            self.favourites.pop(address, None)
        self._save()

    def note_opened(self, address: str) -> None:
        self.opened[address] = int(self.opened.get(address, 0)) + 1
        self._save()

    # -- local re-sorting
    def linked_from_saved(self, saved_texts: list[str]) -> set[str]:
        """Addresses that your favourite or saved pages link to."""
        out: set[str] = set()
        for text in saved_texts:
            out.update(LINK.findall(text))
        return out

    def rerank(self, results: list[dict], saved_texts: list[str]) -> list[dict]:
        """Followed authors first, then pages your saved pages link to, then
        pages you open most, then the vault's own order."""
        linked = self.linked_from_saved(saved_texts)

        def key(item):
            i, r = item
            aid = r["a"].split(":", 1)[1].split("/", 1)[0]
            return (-(aid in self.follows), -(r["a"] in self.favourites),
                    -(r["a"] in linked), -int(self.opened.get(r["a"], 0)), i)
        return [r for _, r in sorted(enumerate(results), key=key)]
