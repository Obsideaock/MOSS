"""Wire format for MOSS request/response.

Everything fits in one 163-byte MeshCore channel datagram. The first four
types are exactly what page_transfer.py already speaks, so a page sent by the
older script is still understood; the last three are new.

    MANIFEST  gateway -> client   how many chunks, how long, hash, name
    CHUNK     gateway -> client   one numbered slice of the (zlib) bytes
    NACK      client  -> gateway  chunk numbers the client is still missing
    DONE      client  -> gateway  hash matched, you can stop
    REQUEST   client  -> gateway  "search for X" / "send me article X"
    STATUS    gateway -> client   queued / fetching from the web / sending
    ERROR     gateway -> client   no such article, gateway busy, ...

A request id (rid) is picked by the client and doubles as the transfer id of
the answer, so a client can ignore every other conversation on the channel.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

MOSS_DATA_TYPE = 0xFF01
MAX_DATAGRAM = 163

MANIFEST, CHUNK, NACK, DONE, REQUEST, STATUS, ERROR = 1, 2, 3, 4, 5, 6, 7

CHUNK_HEADER = 5                              # type(1) + tid(2) + index(2)
CHUNK_BYTES = MAX_DATAGRAM - CHUNK_HEADER     # 158 bytes of page per datagram
NACK_ROOM = (MAX_DATAGRAM - 4) // 2           # indices per NACK datagram

FLAG_ZLIB = 1

KIND_SEARCH, KIND_ARTICLE = 1, 2
KIND_NAMES = {KIND_SEARCH: "search", KIND_ARTICLE: "article"}

ST_QUEUED, ST_FETCHING, ST_SENDING = 1, 2, 3

MAX_QUERY_BYTES = MAX_DATAGRAM - 4


def short_hash(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()[:8]


# ---------------------------------------------------------------- manifest

def build_manifest(tid: int, total: int, length: int, digest: bytes,
                   name: str, flags: int) -> bytes:
    name_bytes = name.encode()[:100]
    return (struct.pack("<BHHIB", MANIFEST, tid, total, length, flags)
            + digest + bytes([len(name_bytes)]) + name_bytes)


def parse_manifest(raw: bytes) -> dict:
    _, tid, total, length, flags = struct.unpack("<BHHIB", raw[:10])
    digest = raw[10:18]
    name_len = raw[18]
    return {"tid": tid, "total": total, "length": length, "flags": flags,
            "hash": digest,
            "name": raw[19:19 + name_len].decode(errors="replace")}


# ---------------------------------------------------------------- chunk / nack / done

def build_chunk(tid: int, index: int, payload: bytes) -> bytes:
    return struct.pack("<BHH", CHUNK, tid, index) + payload


def parse_chunk(raw: bytes) -> tuple[int, int, bytes]:
    _, tid, index = struct.unpack("<BHH", raw[:CHUNK_HEADER])
    return tid, index, raw[CHUNK_HEADER:]


def build_nack(tid: int, missing: list[int]) -> bytes:
    chosen = missing[:NACK_ROOM]
    return (struct.pack("<BHB", NACK, tid, len(chosen))
            + b"".join(struct.pack("<H", i) for i in chosen))


def parse_nack(raw: bytes) -> tuple[int, list[int]]:
    _, tid, count = struct.unpack("<BHB", raw[:4])
    return tid, [struct.unpack("<H", raw[4 + 2 * i:6 + 2 * i])[0]
                 for i in range(count)]


def build_done(tid: int) -> bytes:
    return struct.pack("<BH", DONE, tid)


# ---------------------------------------------------------------- request / status / error

def build_request(rid: int, kind: int, query: str) -> bytes:
    body = query.strip().encode()[:MAX_QUERY_BYTES]
    # Never cut a UTF-8 character in half.
    body = body.decode(errors="ignore").encode()
    return struct.pack("<BHB", REQUEST, rid, kind) + body


def parse_request(raw: bytes) -> tuple[int, int, str]:
    _, rid, kind = struct.unpack("<BHB", raw[:4])
    return rid, kind, raw[4:].decode(errors="replace")


def build_status(rid: int, state: int, value: int = 0) -> bytes:
    """value: queue position (QUEUED), 0 (FETCHING), eta seconds (SENDING)."""
    return struct.pack("<BHBH", STATUS, rid, state, min(value, 0xFFFF))


def parse_status(raw: bytes) -> tuple[int, int, int]:
    _, rid, state, value = struct.unpack("<BHBH", raw[:6])
    return rid, state, value


def build_error(rid: int, message: str) -> bytes:
    body = message.encode()[:MAX_DATAGRAM - 3]
    body = body.decode(errors="ignore").encode()
    return struct.pack("<BH", ERROR, rid) + body


def parse_error(raw: bytes) -> tuple[int, str]:
    _, rid = struct.unpack("<BH", raw[:3])
    return rid, raw[3:].decode(errors="replace")


def peek_tid(raw: bytes) -> int | None:
    """The transfer/request id of any datagram, or None if it's too short."""
    return struct.unpack("<H", raw[1:3])[0] if len(raw) >= 3 else None


# ---------------------------------------------------------------- search results

@dataclass
class SearchResult:
    title: str
    blurb: str = ""


def encode_results(results: list[SearchResult]) -> bytes:
    """One result per line, tab-separated. Tiny, human-readable, no JSON tax."""
    lines = []
    for r in results:
        title = r.title.replace("\t", " ").replace("\n", " ")
        blurb = r.blurb.replace("\t", " ").replace("\n", " ")
        lines.append(f"{title}\t{blurb}")
    return "\n".join(lines).encode()


def decode_results(data: bytes) -> list[SearchResult]:
    out = []
    for line in data.decode(errors="replace").splitlines():
        if not line.strip():
            continue
        title, _, blurb = line.partition("\t")
        out.append(SearchResult(title.strip(), blurb.strip()))
    return out
