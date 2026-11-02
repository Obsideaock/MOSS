"""Spore prints: a Seed Vault's family tree, back to the original.

    "3 hops from the original Seed Vault at ATLAS, CU Boulder:
     ATLAS Seed Vault -> Lafayette -> Longmont -> here, since 2026-12-04"

The original vault's owner writes one note, once (`moss spore-print "..."`),
signed with the vault's own key. Installs never make one. Other vaults get
the print from readers who carry it (and later from other vaults): the first
valid print a vault receives is adopted, with one more link signed by the
adopting vault, and never changes after.

    root    {key, x, name, note, time}           signed by the root vault
    link    {from, to, x, name, via, time}       signed by the vault "to",
                                                 over the previous signature too,
                                                 so links can't be cut or reordered

Nothing in a print names a reader: a link says it was carried "via a reader",
never which one, so prints can't show where someone has been.

Anyone can start their own tree with their own vault; the original is
recognised by its root fingerprint (KNOWN_ORIGINALS).
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

from Crypto.Signature import eddsa

from .seal import fingerprint

ROOT_CONTEXT = b"MOSS spore print root\x00"
LINK_CONTEXT = b"MOSS spore print link\x00"
MAX_LINKS = 64
MAX_NOTE = 200
MAX_NAME = 60
MAX_BYTES = 40_000

# Root fingerprints of well-known original vaults -> how readers label them.
# The ATLAS vault's goes here once its owner has made its print.
KNOWN_ORIGINALS: dict[str, str] = {
    "00B2-ACDE-637D-7729": "ATLAS, CU Boulder",          # the first Seed Vault, since 2026-11-02
}


class PrintError(Exception):
    pass


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sign(keys, msg: bytes) -> str:
    return keys.sign(msg).hex()


def _verify(ed_hex: str, msg: bytes, sig_hex: str) -> None:
    try:
        eddsa.new(eddsa.import_public_key(bytes.fromhex(ed_hex)), "rfc8032").verify(msg, bytes.fromhex(sig_hex))
    except (ValueError, TypeError) as exc:
        raise PrintError("a signature in the spore print doesn't check out") from exc


def _vault_fp(entry: dict) -> str:
    return fingerprint(bytes.fromhex(entry["x"]), bytes.fromhex(entry["key"] if "key" in entry else entry["to"]))


# ---------------------------------------------------------------- making and growing

def make_root(keys, note: str, when: int | None = None) -> dict:
    note = " ".join(note.split())
    if not note or len(note) > MAX_NOTE:
        raise PrintError(f"the note must be 1-{MAX_NOTE} characters")
    root = {"key": keys.ed_pub.hex(), "x": keys.x_pub.hex(), "name": keys.name[:MAX_NAME],
            "note": note, "time": int(when or time.time())}
    return {"moss": 1, "root": root, "root_sig": _sign(keys, ROOT_CONTEXT + _canonical(root)),
            "links": []}


def adopt(sp: dict, keys, via: str = "reader", when: int | None = None) -> dict:
    """The print as this vault will keep it: verified, plus our own link."""
    verify(sp)
    mine = keys.ed_pub.hex()
    if sp["root"]["key"] == mine or any(link["to"] == mine for link in sp["links"]):
        raise PrintError("that print already includes this vault")
    if len(sp["links"]) >= MAX_LINKS:
        raise PrintError("that print's family tree is too long")
    prev_key = sp["links"][-1]["to"] if sp["links"] else sp["root"]["key"]
    prev_sig = sp["links"][-1]["sig"] if sp["links"] else sp["root_sig"]
    link = {"from": prev_key, "to": mine, "x": keys.x_pub.hex(), "name": keys.name[:MAX_NAME],
            "via": via if via in ("reader", "vault") else "reader", "time": int(when or time.time())}
    signed = dict(link, sig=_sign(keys, LINK_CONTEXT + _canonical(link) + bytes.fromhex(prev_sig)))
    out = copy.deepcopy(sp)
    out["links"].append(signed)
    return out


def verify(sp: dict) -> None:
    """Raises PrintError unless every signature checks and the chain is unbroken."""
    try:
        if sp.get("moss") != 1 or set(sp) != {"moss", "root", "root_sig", "links"}:
            raise PrintError("not a spore print")
        root = sp["root"]
        if set(root) != {"key", "x", "name", "note", "time"}:
            raise PrintError("spore print root is malformed")
        if len(root["note"]) > MAX_NOTE or len(root["name"]) > MAX_NAME:
            raise PrintError("spore print root is too long")
        _verify(root["key"], ROOT_CONTEXT + _canonical(root), sp["root_sig"])
        prev_key, prev_sig = root["key"], sp["root_sig"]
        seen = {root["key"]}
        if not isinstance(sp["links"], list) or len(sp["links"]) > MAX_LINKS:
            raise PrintError("spore print has too many links")
        for link in sp["links"]:
            body = {k: v for k, v in link.items() if k != "sig"}
            if set(body) != {"from", "to", "x", "name", "via", "time"} or len(link["name"]) > MAX_NAME:
                raise PrintError("a spore print link is malformed")
            if link["from"] != prev_key or link["to"] in seen:
                raise PrintError("the spore print's chain is broken")
            _verify(link["to"], LINK_CONTEXT + _canonical(body) + bytes.fromhex(prev_sig), link["sig"])
            seen.add(link["to"])
            prev_key, prev_sig = link["to"], link["sig"]
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        if isinstance(exc, PrintError):
            raise
        raise PrintError("not a valid spore print") from exc


def to_bytes(sp: dict) -> bytes:
    return _canonical(sp)


def from_bytes(data: bytes) -> dict:
    if len(data) > MAX_BYTES:
        raise PrintError("spore print too big")
    try:
        sp = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PrintError("not a spore print") from exc
    verify(sp)
    return sp


# ---------------------------------------------------------------- reading it

def summary(sp: dict) -> dict:
    """What readers are shown (all of it public)."""
    root = sp["root"]
    origin = fingerprint(bytes.fromhex(root["x"]), bytes.fromhex(root["key"]))
    known = KNOWN_ORIGINALS.get(origin, "")
    chain = [known or root["name"]] + [link["name"] for link in sp["links"]]
    since = sp["links"][-1]["time"] if sp["links"] else root["time"]
    return {"hops": len(sp["links"]), "note": root["note"], "origin": origin,
            "known": known, "chain": chain,
            "since": time.strftime("%Y-%m-%d", time.gmtime(since))}


def describe(s: dict) -> str:
    """One readable line from summary()."""
    if s["hops"] == 0:
        who = f"the original Seed Vault at {s['known']}" if s["known"] else f"an original: {s['note']}"
        return f"{who} ({s['origin']}), since {s['since']}"
    hops = f"{s['hops']} hop{'s' if s['hops'] != 1 else ''}"
    whence = f"the original Seed Vault at {s['known']}" if s["known"] else f"an original ({s['note']})"
    return f"{hops} from {whence}: {' → '.join(s['chain'])}, since {s['since']}"


# ---------------------------------------------------------------- where it's kept

class VaultPrint:
    """A Seed Vault's own print: <keys folder>/spore-print.json."""

    def __init__(self, folder: Path):
        self.path = Path(folder).expanduser() / "spore-print.json"

    def get(self) -> dict | None:
        try:
            return from_bytes(self.path.read_bytes())
        except (OSError, PrintError):
            return None

    def save(self, sp: dict) -> None:
        verify(sp)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(to_bytes(sp))
        tmp.replace(self.path)

    def start_original(self, keys, note: str) -> dict:
        if self.get() is not None:
            raise PrintError("this vault already has a spore print; it never changes")
        sp = make_root(keys, note)
        self.save(sp)
        return sp

    def offered(self, data: bytes, keys) -> str:
        """A reader brought a print. Adopt it if we have none. Returns what happened."""
        if self.get() is not None:
            return "this Seed Vault already has a spore print"
        sp = adopt(from_bytes(data), keys, via="reader")
        self.save(sp)
        return f"adopted: {describe(summary(sp))}"


class CarriedPrints:
    """Prints a reader has picked up from vaults it met, to pass on to vaults
    that have none: ~/.moss/spore-prints/. The shortest chain per origin is kept.
    Also the reader's own print (the first one it met), shown only locally."""

    def __init__(self, home: Path):
        self.folder = Path(home).expanduser() / "spore-prints"

    def _path(self, origin: str) -> Path:
        return self.folder / f"{origin.replace('-', '')}.json"

    def carry(self, sp: dict) -> bool:
        """Keep it if it's new, or a shorter route to the same origin. True if kept."""
        verify(sp)
        origin = summary(sp)["origin"]
        current = self.best(origin)
        if current is not None and len(current["links"]) <= len(sp["links"]):
            return False
        self.folder.mkdir(parents=True, exist_ok=True)
        self._path(origin).write_bytes(to_bytes(sp))
        mine = self.folder / "mine.json"
        if not mine.exists():
            mine.write_bytes(to_bytes(sp))
        return True

    def best(self, origin: str | None = None) -> dict | None:
        """The shortest print carried (for one origin, or any)."""
        found = []
        for f in self.folder.glob("*.json") if self.folder.is_dir() else []:
            if f.name == "mine.json":
                continue
            try:
                sp = from_bytes(f.read_bytes())
            except (OSError, PrintError):
                continue
            if origin is None or summary(sp)["origin"] == origin:
                found.append(sp)
        known = [sp for sp in found if summary(sp)["known"]]
        pool = known or found
        return min(pool, key=lambda sp: len(sp["links"])) if pool else None

    def mine(self) -> dict | None:
        try:
            return from_bytes((self.folder / "mine.json").read_bytes())
        except (OSError, PrintError):
            return None
