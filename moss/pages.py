"""Community pages, stage 1: author identities and signed page records.

Nothing here touches the radio yet. Stage 2 (uploading) sends the finished
.mosspage files this module makes; stage 3 lets readers find them.

Identities
    An author is an Ed25519 signing key. One reader can hold several, in
    ~/.moss/authors/<author id>/, and the private keys never leave the
    device. An *anonymous* identity is a fresh, nameless key made for one
    page, so nothing links that page to the author's other identities.

Addresses
    moss:<author id>/<name>
    The author id is 26 lowercase base32 characters: 128 bits of a hash of
    the author's public key. That's long enough that nobody can make a
    second key that claims the same addresses. The short fingerprint
    (3F9A-12C4-77E0-5B21, same style as a Seed Vault's) is for comparing
    by eye. Public pages have readable names (water-filters); unlisted
    pages get a random one (~k7q2m9xw4t3pb6c8) so a link doesn't give its
    topic away.

The page file (.mosspage), the exact bytes that travel and get stored
    MAGIC "MOSSPG1\\n" | record length (4 bytes) | record | signature (64) | body

    record     canonical JSON (sorted keys, no spaces), signed as is:
               address, key, author, title, about, version, time,
               visibility, deleted, lock, format, size, hash
    signature  Ed25519 over "MOSS page v1\\0" + record
    body       zlib-compressed Markdown (empty for a deletion)

Readers and vaults accept a page only if the signature checks, the key
matches the address, the record is exactly canonical (so there's only one
valid encoding of any page), and the body matches the record's size and
hash. A lower version than one already seen is refused, so nobody can
replay an old signed copy to undo an author's fix; the author can still
bring old text back by publishing it again as a new version.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import secrets
import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from . import protocol as P
from .config import moss_home  # noqa: F401  (re-exported)

MAGIC = b"MOSSPG1\n"
SIGN_CONTEXT = b"MOSS page v1\x00"
FORMAT = 1
SIG_LEN = 64
MAX_RECORD = 2048
MAX_TEXT = 2_000_000              # decompressed Markdown; anything bigger is refused
DEFAULT_CAP = 250                 # spores; each vault owner can change theirs
PUBLIC, UNLISTED = "public", "unlisted"

NAME_RULE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
RANDOM_RULE = re.compile(r"^~[a-z2-7]{16}$")
ADDRESS_RULE = re.compile(r"^moss:([a-z2-7]{26})/(~?[a-z0-9-]{1,48})$")
RECORD_KEYS = {"moss", "address", "key", "author", "title", "about", "version", "time",
               "visibility", "deleted", "lock", "format", "size", "hash"}
LIMITS = {"author": 40, "title": 120, "about": 200}


class PageError(Exception):
    """A page that must not be trusted, or a request that can't be done."""




# ---------------------------------------------------------------- ids and addresses

def _key_hash(ed_pub: bytes) -> bytes:
    return hashlib.sha256(b"MOSS author" + ed_pub).digest()


def author_id(ed_pub: bytes) -> str:
    """26 characters, 128 bits: the part of an address that names its author."""
    return base64.b32encode(_key_hash(ed_pub)[:16]).decode().lower().rstrip("=")


def author_fingerprint(ed_pub: bytes) -> str:
    h = _key_hash(ed_pub)[:8].hex().upper()
    return "-".join(h[i:i + 4] for i in range(0, 16, 4))


def random_name() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz234567"
    return "~" + "".join(secrets.choice(alphabet) for _ in range(16))   # 80 random bits


def check_name(name: str, visibility: str) -> str:
    if RANDOM_RULE.match(name):
        if visibility != UNLISTED:
            raise PageError("names starting with ~ are random names for unlisted pages")
        return name
    if not NAME_RULE.match(name):
        raise PageError(f"page name {name!r}: use lowercase letters, digits and dashes "
                        f"(up to 48), like water-filters")
    return name


def parse_address(address: str) -> tuple[str, str]:
    m = ADDRESS_RULE.match(address.strip())
    if not m:
        raise PageError(f"not a MOSS page address: {address!r} "
                        f"(they look like moss:<26 letters and digits>/<name>)")
    return m.group(1), m.group(2)


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48].strip("-")
    return s or "page"


def spores_for(size: int) -> int:
    return max(1, math.ceil(size / P.CHUNK_BYTES))


# ---------------------------------------------------------------- the record

def _canonical(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _no_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise PageError(f"record repeats the field {k!r}")
        out[k] = v
    return out


def _check_record(rec: dict) -> None:
    if set(rec) != RECORD_KEYS:
        extra, missing = set(rec) - RECORD_KEYS, RECORD_KEYS - set(rec)
        raise PageError(f"record fields wrong (extra {sorted(extra)}, missing {sorted(missing)})")
    if rec["moss"] != FORMAT:
        raise PageError(f"page format {rec['moss']!r} isn't supported by this version of MOSS")
    for k in ("address", "key", "author", "title", "about", "visibility", "hash"):
        if not isinstance(rec[k], str):
            raise PageError(f"record field {k} must be text")
    for k, n in LIMITS.items():
        if len(rec[k]) > n or "\n" in rec[k]:
            raise PageError(f"record field {k} is longer than {n} characters or has a line break")
    for k in ("version", "time", "size"):
        if type(rec[k]) is not int or rec[k] < 0:
            raise PageError(f"record field {k} must be a whole number")
    if not 1 <= rec["version"] < 2 ** 31:
        raise PageError("version must be between 1 and 2^31")
    if type(rec["deleted"]) is not bool:
        raise PageError("record field deleted must be true or false")
    if rec["lock"] is not None:
        raise PageError("locked pages arrive in a later version of MOSS")
    if rec["format"] != "zlib-markdown":
        raise PageError(f"unknown body format {rec['format']!r}")
    if rec["visibility"] not in (PUBLIC, UNLISTED):
        raise PageError(f"visibility must be public or unlisted, not {rec['visibility']!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", rec["key"]) or not re.fullmatch(r"[0-9a-f]{64}", rec["hash"]):
        raise PageError("key and hash must be 64 hex digits")
    aid, name = parse_address(rec["address"])
    check_name(name, rec["visibility"])
    if aid != author_id(bytes.fromhex(rec["key"])):
        raise PageError("the signing key doesn't own this address (someone else's page, re-signed?)")
    if rec["deleted"] and rec["size"] != 0:
        raise PageError("a deletion carries no body")


@dataclass
class Page:
    record: dict
    record_bytes: bytes
    signature: bytes
    body: bytes

    # -- reading
    @property
    def address(self) -> str:
        return self.record["address"]

    @property
    def version(self) -> int:
        return self.record["version"]

    @property
    def deleted(self) -> bool:
        return self.record["deleted"]

    @property
    def key(self) -> bytes:
        return bytes.fromhex(self.record["key"])

    @property
    def fingerprint(self) -> str:
        return author_fingerprint(self.key)

    @property
    def spores(self) -> int:
        return spores_for(len(self.to_bytes()))

    def text(self) -> str:
        if self.deleted:
            return ""
        d = zlib.decompressobj()
        try:
            out = d.decompress(self.body, MAX_TEXT + 1)
        except zlib.error as exc:
            raise PageError("page body isn't valid compressed text") from exc
        if len(out) > MAX_TEXT or d.unconsumed_tail:
            raise PageError(f"page text is over {MAX_TEXT:,} bytes")
        try:
            return out.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PageError("page text isn't valid UTF-8") from exc

    def to_bytes(self) -> bytes:
        return (MAGIC + struct.pack("<I", len(self.record_bytes)) + self.record_bytes
                + self.signature + self.body)

    def describe(self) -> str:
        r = self.record
        who = r["author"] or "anonymous"
        state = "DELETED" if r["deleted"] else f"{r['title']!r}"
        return (f"{r['address']}\n  v{r['version']} {state} by {who} ({self.fingerprint}), "
                f"{r['visibility']}, {len(self.to_bytes()):,} bytes = {self.spores} spores, "
                f"signed {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['time']))}")


def load(data: bytes) -> Page:
    """Parse and fully verify a page file. Raises PageError if anything is off."""
    if not data.startswith(MAGIC):
        raise PageError("not a MOSS page file")
    pos = len(MAGIC)
    if len(data) < pos + 4:
        raise PageError("page file cut short")
    (n,) = struct.unpack_from("<I", data, pos)
    pos += 4
    if n > MAX_RECORD:
        raise PageError("page record too long")
    if len(data) < pos + n + SIG_LEN:
        raise PageError("page file cut short")
    record_bytes = data[pos:pos + n]
    signature = data[pos + n:pos + n + SIG_LEN]
    body = data[pos + n + SIG_LEN:]
    try:
        rec = json.loads(record_bytes.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, PageError):
            raise
        raise PageError("page record isn't valid") from exc
    if not isinstance(rec, dict):
        raise PageError("page record isn't valid")
    _check_record(rec)
    if _canonical(rec) != record_bytes:
        raise PageError("page record isn't in canonical form")
    try:
        pub = eddsa.import_public_key(bytes.fromhex(rec["key"]))
        eddsa.new(pub, "rfc8032").verify(SIGN_CONTEXT + record_bytes, signature)
    except ValueError as exc:
        raise PageError("signature doesn't match: this page was altered or forged") from exc
    if len(body) != rec["size"] or hashlib.sha256(body).hexdigest() != rec["hash"]:
        raise PageError("page body doesn't match its signed record: altered after signing")
    page = Page(rec, record_bytes, signature, body)
    page.text()                                     # bad compression or too big: refuse now
    return page


def check_newer(current: Page | None, incoming: Page) -> str:
    """What to do with an incoming copy of an address we may already hold.
    Returns 'new', 'newer' or 'same'; raises PageError for a replay."""
    if current is None:
        return "new"
    if current.address != incoming.address:
        raise PageError("different addresses")
    if incoming.version < current.version:
        raise PageError(f"older version (v{incoming.version}) of a page already seen at "
                        f"v{current.version}: refused, so an old copy can't undo a newer one")
    if incoming.version == current.version:
        if incoming.record_bytes != current.record_bytes or incoming.body != current.body:
            raise PageError(f"two different pages both signed as v{incoming.version}: refused")
        return "same"
    return "newer"


class SeenVersions:
    """The newest version this reader has accepted for each address, so a
    vault can't hand it an older copy later. ~/.moss/seen-pages.json."""

    def __init__(self, home: Path | None = None):
        self.path = (home or moss_home()) / "seen-pages.json"
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def accept(self, page: Page) -> None:
        seen = self.data.get(page.address)
        digest = hashlib.sha256(page.to_bytes()).hexdigest()
        if seen:
            if page.version < seen["v"]:
                raise PageError(f"this vault sent v{page.version} of a page you've already "
                                f"seen at v{seen['v']}: refused (an old copy, replayed?)")
            if page.version == seen["v"] and digest != seen["h"]:
                raise PageError(f"two different pages both signed as v{page.version}: refused")
        self.data[page.address] = {"v": page.version, "h": digest}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


# ---------------------------------------------------------------- authors

def _private_dir(folder: Path) -> None:
    """Create folder (and missing parents) readable only by this user."""
    missing = []
    while not folder.exists():
        missing.append(folder)
        folder = folder.parent
    for d in reversed(missing):
        d.mkdir(mode=0o700, exist_ok=True)


def _private_write(path: Path, text: str) -> None:
    _private_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _first_heading(md: str) -> str | None:
    for line in md.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def _first_sentence(md: str) -> str:
    for block in re.split(r"\n\s*\n", md):
        b = block.strip()
        if not b or b[0] in "#|:>-*`!" or b.startswith(":::"):
            continue
        b = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", b)        # [[link|text]] -> text
        b = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", b)               # [text](url) -> text
        b = re.sub(r"[*_`$]", "", " ".join(b.split()))
        m = re.match(r"(.+?[.!?])(\s|$)", b)
        s = m.group(1) if m else b
        return s if len(s) <= LIMITS["about"] else s[:LIMITS["about"] - 1].rstrip() + "…"
    return ""


class Identity:
    """One author key, with what this device remembers about its pages."""

    def __init__(self, folder: Path, key: ECC.EccKey, meta: dict):
        self.folder, self.key, self.meta = folder, key, meta
        self.pub = key.public_key().export_key(format="raw")
        self.id = author_id(self.pub)
        self.fingerprint = author_fingerprint(self.pub)

    @property
    def name(self) -> str:
        return self.meta.get("name", "")

    @property
    def anonymous(self) -> bool:
        return bool(self.meta.get("anonymous"))

    def label(self) -> str:
        if self.anonymous:
            note = self.meta.get("note")
            return f"anonymous{f' (for {note})' if note else ''}"
        return self.name

    def _save_meta(self) -> None:
        _private_write(self.folder / "identity.json", json.dumps(self.meta, indent=1))

    # -- what this device has published
    def _index(self) -> dict:
        try:
            return json.loads((self.folder / "pages.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_index(self, index: dict) -> None:
        _private_write(self.folder / "pages.json", json.dumps(index, indent=1))

    def published(self) -> dict:
        """name -> {version, visibility, title, deleted}"""
        return self._index()

    def history(self, name: str) -> list[int]:
        folder = self.folder / "history" / name
        return sorted(int(p.stem[1:]) for p in folder.glob("v*.md")) if folder.is_dir() else []

    def old_text(self, name: str, version: int) -> str:
        path = self.folder / "history" / name / f"v{version}.md"
        if not path.exists():
            raise PageError(f"no saved copy of {name} v{version} on this device "
                            f"(saved versions: {self.history(name) or 'none'})")
        return path.read_text(encoding="utf-8")

    # -- signing
    def sign(self, name: str, markdown: str | None, *, title: str | None = None,
             about: str | None = None, visibility: str = PUBLIC,
             version: int | None = None, when: int | None = None) -> Page:
        """Make and sign a page (markdown None = a deletion). Records it in
        this identity's history; returns the verified Page."""
        if visibility not in (PUBLIC, UNLISTED):
            raise PageError("visibility must be public or unlisted")
        check_name(name, visibility)
        index = self._index()
        prev = index.get(name)
        if prev and prev["visibility"] != visibility and markdown is not None:
            raise PageError(f"{name} was published {prev['visibility']}; a page keeps its "
                            f"visibility (publish under a new name to change it)")
        version = version or (prev["version"] + 1 if prev else 1)
        if prev and version <= prev["version"]:
            raise PageError(f"{name} is already at v{prev['version']}; the next version is "
                            f"v{prev['version'] + 1}")
        deleted = markdown is None
        if deleted:
            if not prev:
                raise PageError(f"nothing published as {name} from this identity")
            visibility = prev["visibility"]
            body, title, about = b"", "", ""
        else:
            title = (title or _first_heading(markdown) or name).strip()
            about = (about if about is not None else _first_sentence(markdown)).strip()
            body = zlib.compress(markdown.encode("utf-8"), 9)
        for k, v in (("title", title), ("about", about)):
            if "\n" in v or len(v) > LIMITS[k]:
                raise PageError(f"{k} must be one line of at most {LIMITS[k]} characters")
        author = "" if self.anonymous else self.name[:LIMITS["author"]]
        rec = {"moss": FORMAT, "address": f"moss:{self.id}/{name}", "key": self.pub.hex(),
               "author": author, "title": title, "about": about, "version": version,
               "time": int(when if when is not None else time.time()),
               "visibility": visibility, "deleted": deleted, "lock": None,
               "format": "zlib-markdown", "size": len(body),
               "hash": hashlib.sha256(body).hexdigest()}
        _check_record(rec)
        rb = _canonical(rec)
        sig = eddsa.new(self.key, "rfc8032").sign(SIGN_CONTEXT + rb)
        page = load(Page(rec, rb, sig, body).to_bytes())         # verify our own work
        if not deleted:
            hist = self.folder / "history" / name / f"v{version}.md"
            _private_write(hist, markdown)
        index[name] = {"version": version, "visibility": visibility, "title": title,
                       "deleted": deleted, "time": rec["time"]}
        self._save_index(index)
        return page


class Authors:
    """All the identities on this device: ~/.moss/authors/."""

    def __init__(self, home: Path | None = None):
        self.home = home or moss_home()
        self.folder = self.home / "authors"

    def all(self) -> list[Identity]:
        out = []
        if self.folder.is_dir():
            for d in sorted(self.folder.iterdir()):
                if (d / "key.pem").exists():
                    out.append(self._load(d))
        return sorted(out, key=lambda i: (i.anonymous, i.meta.get("created", 0)))

    def _load(self, d: Path) -> Identity:
        key = ECC.import_key((d / "key.pem").read_text())
        try:
            meta = json.loads((d / "identity.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        ident = Identity(d, key, meta)
        if ident.id != d.name:
            raise PageError(f"{d} holds a key that doesn't match its folder name")
        return ident

    def _add(self, key: ECC.EccKey, meta: dict) -> Identity:
        pub = key.public_key().export_key(format="raw")
        d = self.folder / author_id(pub)
        if (d / "key.pem").exists():
            raise PageError(f"that identity is already on this device ({author_id(pub)})")
        _private_write(d / "key.pem", key.export_key(format="PEM"))
        ident = Identity(d, key, meta)
        ident._save_meta()
        return ident

    def create(self, name: str = "", anonymous: bool = False, note: str = "") -> Identity:
        name = " ".join(name.split())
        if not anonymous and not name:
            raise PageError("an identity needs a display name (or make it anonymous)")
        if len(name) > LIMITS["author"]:
            raise PageError(f"display names are at most {LIMITS['author']} characters")
        meta = {"name": "" if anonymous else name, "anonymous": anonymous,
                "created": int(time.time())}
        if note:
            meta["note"] = note
        ident = self._add(ECC.generate(curve="Ed25519"), meta)
        if not anonymous and self.default() is None:
            self.set_default(ident)
        return ident

    def find(self, what: str) -> Identity:
        """By author id (or its start), fingerprint, or display name."""
        what = what.strip()
        if what.startswith("moss:"):
            what = parse_address(what)[0]
        hits = [i for i in self.all()
                if i.id.startswith(what.lower()) or i.fingerprint == what.upper()
                or (i.name and i.name.lower() == what.lower())]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise PageError(f"no identity on this device matches {what!r} (see: moss author list)")
        raise PageError(f"{len(hits)} identities match {what!r}; use more of the id")

    def default(self) -> Identity | None:
        try:
            return self.find((self.folder / "default").read_text().strip())
        except (OSError, PageError):
            return None

    def set_default(self, ident: Identity) -> None:
        if ident.anonymous:
            raise PageError("an anonymous identity can't be the default")
        _private_write(self.folder / "default", ident.id)

    def owner_of(self, address: str) -> Identity:
        aid, _ = parse_address(address)
        d = self.folder / aid
        if not (d / "key.pem").exists():
            raise PageError(f"{address} isn't from an identity on this device")
        return self._load(d)

    # -- backups
    def backup(self, ident: Identity, path: Path, passphrase: str) -> None:
        if len(passphrase) < 10:
            raise PageError("use a passphrase of at least 10 characters (a few random words)")
        pem = ident.key.export_key(format="PEM", passphrase=passphrase,
                                   protection="scryptAndAES256-CBC")
        data = {"moss-author-backup": 1, "id": ident.id, "meta": ident.meta, "key": pem}
        _private_write(Path(path), json.dumps(data, indent=1))

    def restore(self, path: Path, passphrase: str) -> Identity:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            pem = data["key"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PageError(f"{path} isn't a MOSS author backup") from exc
        try:
            key = ECC.import_key(pem, passphrase=passphrase)
        except (ValueError, IndexError, TypeError) as exc:
            raise PageError("wrong passphrase, or the backup is damaged") from exc
        if author_id(key.public_key().export_key(format="raw")) != data.get("id"):
            raise PageError("the backup's key doesn't match its id")
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        ident = self._add(key, meta)
        if not ident.anonymous and self.default() is None:
            self.set_default(ident)
        return ident


# ---------------------------------------------------------------- outbox

def outbox(home: Path | None = None) -> Path:
    return (home or moss_home()) / "outbox"


def save_to_outbox(page: Page, home: Path | None = None) -> Path:
    aid, name = parse_address(page.address)
    path = outbox(home) / f"{aid}-{name.lstrip('~')}-v{page.version}.mosspage"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(page.to_bytes())
    return path
