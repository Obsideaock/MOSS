"""Private reading: requests and replies that only the reader and the Seed
Vault can read.

Anyone in radio range can hear MOSS traffic, and the #moss channel's key is
public. So what someone searches for and reads is encrypted end to end, on
top of whatever the channel does.

How it works (the HPKE pattern, RFC 9180, built from standard pieces:
X25519, HKDF-SHA256, ChaCha20-Poly1305):

    The Seed Vault has a long-term key pair. Readers learn its public key
    once (HELLO) and remember it; its fingerprint can be checked by eye.

    Request   The reader makes a fresh one-time key, combines it with the
              vault's public key, and encrypts the search or title. Every
              sealed request is exactly 163 bytes, so its length says nothing.

    Reply     The vault makes its own one-time key. The capsule and every
              spore are encrypted under a key mixed from both one-time keys
              and the vault's long-term key, then the one-time keys are
              thrown away. Only the real vault can produce that key (so a fake
              vault can't feed you pages), and someone who steals the Pi later
              still can't read past replies.

What a listener still learns: that someone used MOSS, when, and roughly how
big the page was (the spore count). The Seed Vault can round page sizes up
to hide that last part (pad_sizes, off by default; ~10% more airtime).

Wire formats (all fit one 163-byte datagram):

    HELLO     8  | x25519 pub 32 | ed25519 pub 32 | signature 64 | name
    WHOIS     9  | rid 2
    SREQUEST  10 | rid 2 | reader one-time pub 32 | sealed(kind, len, query, padding) | tag 16
    SCAPSULE  11 | rid 2 | vault one-time pub 32  | sealed(total, wire len, body len, flags, hash) | tag 16
    SERROR    12 | rid 2 | sealed(message) | tag 16
    spores    ordinary CHUNKs carrying the encrypted body; its 16-byte tag rides at the end
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path

from Crypto.Cipher import ChaCha20, ChaCha20_Poly1305
from Crypto.Hash import SHA256
from Crypto.Protocol.DH import key_agreement
from Crypto.Protocol.KDF import HKDF
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from . import protocol as P

HELLO, WHOIS, SREQUEST, SCAPSULE, SERROR = 8, 9, 10, 11, 12
SUITE = b"MOSS-seal-v1 X25519 HKDF-SHA256 ChaCha20-Poly1305"
TAG = 16
REQ_HEAD = 1 + 2 + 32                       # type, rid, one-time public key
REQ_ROOM = P.MAX_DATAGRAM - REQ_HEAD - TAG  # 112 bytes of sealed request
MAX_SEALED_QUERY = REQ_ROOM - 2             # 110 bytes of search text or title
CAPSULE_BODY = struct.Struct("<HIIB8s")     # total, wire length, body length, flags, hash
NONCE_REQUEST, NONCE_ERROR, NONCE_CAPSULE, NONCE_BODY = (bytes(11) + bytes([n]) for n in range(4))


class SealError(Exception):
    """Not for us, or tampered with."""


# ---------------------------------------------------------------- primitives

def _x_pub(raw: bytes) -> ECC.EccKey:
    if len(raw) != 32:
        raise SealError("bad public key")
    return ECC.construct(curve="curve25519", point_x=int.from_bytes(raw, "little"))


def _raw(key: ECC.EccKey) -> bytes:
    return key.public_key().export_key(format="raw")


def _dh(priv: ECC.EccKey, pub_raw: bytes) -> bytes:
    try:
        return key_agreement(static_priv=priv, static_pub=_x_pub(pub_raw), kdf=lambda z: z)
    except ValueError as exc:                  # e.g. a low-order point
        raise SealError("bad key exchange") from exc


def _hkdf(secret: bytes, salt: bytes, label: bytes) -> bytes:
    return HKDF(secret, 32, salt, SHA256, context=SUITE + b" " + label)


def _seal(key: bytes, nonce: bytes, aad: bytes, plain: bytes) -> bytes:
    c = ChaCha20_Poly1305.new(key=key, nonce=nonce)
    c.update(aad)
    ct, tag = c.encrypt_and_digest(plain)
    return ct + tag


def _open(key: bytes, nonce: bytes, aad: bytes, sealed: bytes) -> bytes:
    if len(sealed) < TAG:
        raise SealError("too short")
    c = ChaCha20_Poly1305.new(key=key, nonce=nonce)
    c.update(aad)
    try:
        return c.decrypt_and_verify(sealed[:-TAG], sealed[-TAG:])
    except ValueError as exc:
        raise SealError("failed authentication") from exc


def fingerprint(x_pub: bytes, ed_pub: bytes) -> str:
    """Short enough to read aloud or print on a sticker: 3F9A-12C4-77E0-5B21."""
    h = hashlib.sha256(b"MOSS seed vault" + x_pub + ed_pub).hexdigest()[:16].upper()
    return "-".join(h[i:i + 4] for i in range(0, 16, 4))


def normalize_fingerprint(text: str) -> str:
    h = "".join(c for c in text.upper() if c in "0123456789ABCDEF")
    if len(h) != 16:
        raise ValueError("a fingerprint is 16 hex digits, like 3F9A-12C4-77E0-5B21")
    return "-".join(h[i:i + 4] for i in range(0, 16, 4))


# ---------------------------------------------------------------- size padding (optional)

def _size_steps():
    s = 1
    while True:
        yield s
        s = max(s + 1, round(s * 1.26))     # about three steps per doubling


def padded_spores(spores: int) -> int:
    """Round a spore count up to the next step: 1 2 3 4 5 6 8 10 13 16 20 25 32 40..."""
    for step in _size_steps():
        if step >= spores:
            return step
    return spores                           # unreachable


# ---------------------------------------------------------------- the Seed Vault's keys

class VaultKeys:
    """The Seed Vault's long-term identity: an X25519 key for encryption and
    an Ed25519 key for signing. Kept in two PEM files readable only by its
    user. Losing them means every reader sees a new fingerprint."""

    def __init__(self, x: ECC.EccKey, ed: ECC.EccKey, name: str = "Seed Vault"):
        self.x, self.ed, self.name = x, ed, name
        self.x_pub, self.ed_pub = _raw(x), _raw(ed)
        self.fingerprint = fingerprint(self.x_pub, self.ed_pub)

    @classmethod
    def load_or_create(cls, folder: str | Path, name: str = "Seed Vault") -> "VaultKeys":
        folder = Path(folder).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        xf, ef = folder / "x25519.pem", folder / "ed25519.pem"
        if xf.exists() and ef.exists():
            return cls(ECC.import_key(xf.read_text()), ECC.import_key(ef.read_text()), name)
        keys = cls(ECC.generate(curve="curve25519"), ECC.generate(curve="Ed25519"), name)
        for path, key in ((xf, keys.x), (ef, keys.ed)):
            path.write_text(key.export_key(format="PEM"))
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        return keys

    def sign(self, message: bytes) -> bytes:
        return eddsa.new(self.ed, "rfc8032").sign(message)

    def hello(self) -> bytes:
        name = self.name.encode()[:P.MAX_DATAGRAM - 129]
        body = self.x_pub + self.ed_pub
        return bytes([HELLO]) + body + self.sign(b"MOSS hello" + body) + name

    # -- requests
    def open_request(self, raw: bytes) -> tuple[int, int, str, "VaultSession"]:
        """Raises SealError if the request isn't for this vault."""
        if len(raw) != P.MAX_DATAGRAM or raw[0] != SREQUEST:
            raise SealError("not a sealed request")
        head, sealed = raw[:REQ_HEAD], raw[REQ_HEAD:]
        rid = struct.unpack("<H", raw[1:3])[0]
        eph = raw[3:REQ_HEAD]
        static = _dh(self.x, eph)
        k_req = _hkdf(static, eph + self.x_pub, b"request")
        plain = _open(k_req, NONCE_REQUEST, head, sealed)
        kind, n = plain[0], plain[1]
        query = plain[2:2 + n].decode("utf-8", errors="replace")
        return rid, kind, query, VaultSession(self, rid, eph, static, k_req)


def verify_hello(raw: bytes) -> tuple[bytes, bytes, str] | None:
    """(x25519 pub, ed25519 pub, name) from a HELLO whose two keys belong
    together. Whether it's *your* vault is the fingerprint's job."""
    if len(raw) < 129 or raw[0] != HELLO:
        return None
    x_pub, ed_pub, sig = raw[1:33], raw[33:65], raw[65:129]
    try:
        eddsa.new(eddsa.import_public_key(ed_pub), "rfc8032").verify(b"MOSS hello" + x_pub + ed_pub, sig)
    except (ValueError, TypeError):
        return None
    return x_pub, ed_pub, raw[129:].decode("utf-8", errors="replace")


@dataclass
class VaultSession:
    """The vault's side of one sealed request."""
    keys: VaultKeys
    rid: int
    reader_eph: bytes
    static: bytes
    k_req: bytes

    def error(self, message: str) -> bytes:
        head = struct.pack("<BH", SERROR, self.rid)
        room = P.MAX_DATAGRAM - len(head) - TAG
        body = message.encode()[:room].decode(errors="ignore").encode()
        return head + _seal(self.k_req, NONCE_ERROR, head, body)

    def seal_reply(self, name: str, blob: bytes, flags: int, digest: bytes,
                   pad_sizes: bool = False) -> tuple[bytes, list[bytes]]:
        """(capsule datagram, encrypted spore payloads) for one transfer."""
        eph = ECC.generate(curve="curve25519")
        eph_pub = _raw(eph)
        k = _hkdf(self.static + _dh(eph, self.reader_eph),
                  self.reader_eph + eph_pub + self.keys.x_pub, b"reply")
        del eph                                       # one-time: gone after this
        name_b = name.encode()
        body = struct.pack("<H", len(name_b)) + name_b + blob
        spores = -(-(len(body) + TAG) // P.CHUNK_BYTES)
        if pad_sizes:
            spores = padded_spores(spores)
            body_padded = body + bytes(spores * P.CHUNK_BYTES - TAG - len(body))
        else:
            body_padded = body
        wire = _seal(k, NONCE_BODY, b"", body_padded)
        chunks = [wire[i:i + P.CHUNK_BYTES] for i in range(0, len(wire), P.CHUNK_BYTES)]
        head = struct.pack("<BH", SCAPSULE, self.rid) + eph_pub
        info = CAPSULE_BODY.pack(len(chunks), len(wire), len(body), flags, digest)
        return head + _seal(k, NONCE_CAPSULE, head, info), chunks


# ---------------------------------------------------------------- the reader's side

class ReaderSession:
    """The reader's side of one sealed request."""

    def __init__(self, vault_x_pub: bytes, rid: int, kind: int, query: str):
        q = query.strip().encode()[:MAX_SEALED_QUERY]
        q = q.decode(errors="ignore").encode()        # never cut a character in half
        self.rid, self.vault_x_pub = rid, vault_x_pub
        eph = ECC.generate(curve="curve25519")
        self.eph_pub = _raw(eph)
        self.static = _dh(eph, vault_x_pub)
        self._eph = eph                               # needed once more, for the reply
        head = struct.pack("<BH", SREQUEST, rid) + self.eph_pub
        self.k_req = _hkdf(self.static, self.eph_pub + vault_x_pub, b"request")
        plain = bytes([kind, len(q)]) + q
        plain += os.urandom(REQ_ROOM - len(plain))   # every request is the same size
        self.request = head + _seal(self.k_req, NONCE_REQUEST, head, plain)
        assert len(self.request) == P.MAX_DATAGRAM
        self.k_reply: bytes | None = None
        self._stream = None
        self._stream_at = 0

    def open_error(self, raw: bytes) -> str:
        head = raw[:3]
        return _open(self.k_req, NONCE_ERROR, head, raw[3:]).decode(errors="replace")

    def open_capsule(self, raw: bytes) -> dict:
        head, vault_eph = raw[:35], raw[3:35]
        k = _hkdf(self.static + _dh(self._eph, vault_eph),
                  self.eph_pub + vault_eph + self.vault_x_pub, b"reply")
        total, wire_len, body_len, flags, digest = CAPSULE_BODY.unpack(
            _open(k, NONCE_CAPSULE, head, raw[35:]))
        self.k_reply = k
        self._eph = None                              # done with the one-time key
        return {"tid": self.rid, "total": total, "length": wire_len, "body_len": body_len,
                "flags": flags, "hash": digest, "name": "", "sealed": True}

    def peek(self, data: bytes, offset: int) -> bytes:
        """Decrypt part of the body early, for the live preview. Not yet
        verified: the reader only keeps the page once open_body() passes."""
        if self._stream is None or offset != self._stream_at:
            self._stream = ChaCha20.new(key=self.k_reply, nonce=NONCE_BODY)
            self._stream.seek(64 + offset)          # ChaCha20-Poly1305 starts at block 1
        self._stream_at = offset + len(data)
        return self._stream.decrypt(data)

    def open_body(self, wire: bytes, body_len: int) -> tuple[str, bytes]:
        body = _open(self.k_reply, NONCE_BODY, b"", wire)[:body_len]
        n = struct.unpack("<H", body[:2])[0]
        return body[2:2 + n].decode("utf-8", errors="replace"), body[2 + n:]


# ---------------------------------------------------------------- remembering the vault

class Keyring:
    """Which Seed Vault this reader trusts, kept in ~/.moss/vault.json.

    The first vault heard is trusted and remembered (like SSH). If you pin a
    fingerprint first (`moss trust 3F9A-...`), only that vault is accepted.
    A remembered vault that suddenly shows a different key is refused.
    """

    def __init__(self, folder: str | Path | None = None):
        self.path = Path(folder or Path.home() / ".moss").expanduser() / "vault.json"
        self.data = self._read()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def x_pub(self) -> bytes | None:
        return bytes.fromhex(self.data["x"]) if "x" in self.data else None

    @property
    def fingerprint(self) -> str | None:
        return self.data.get("fingerprint") or self.data.get("expect")

    def pin(self, fp: str) -> None:
        self.data = {"expect": normalize_fingerprint(fp)}
        self._write()

    def forget(self) -> None:
        self.data = {}
        self._write()

    def offer(self, x_pub: bytes, ed_pub: bytes, name: str) -> str:
        """A HELLO was heard. Returns 'trusted', 'known', 'mismatch' or 'unexpected'."""
        fp = fingerprint(x_pub, ed_pub)
        if "x" in self.data:
            return "known" if self.data["fingerprint"] == fp else "mismatch"
        if "expect" in self.data and self.data["expect"] != fp:
            return "unexpected"
        self.data = {"x": x_pub.hex(), "ed": ed_pub.hex(), "fingerprint": fp,
                     "name": name, "since": time.strftime("%Y-%m-%d %H:%M")}
        self._write()
        return "trusted"
