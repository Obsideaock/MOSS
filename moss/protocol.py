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
# Uploads run the transfer the other way: the reader sends UCHUNKs and the
# Seed Vault answers with UNACKs (same layouts as CHUNK and NACK).
UCHUNK, UNACK = 14, 15
# Range testing: a reader sends PINGs, the Seed Vault answers each with a PONG
# saying how well it heard it. Not encrypted: they carry nothing but numbers.
PING, PONG = 17, 18
PING_ZERO_HOP = 1                 # flag: sent without repeaters; answer the same way
SIGNAL_UNKNOWN = 127              # rssi/snr byte when the radio didn't say

CHUNK_HEADER = 5                              # type(1) + tid(2) + index(2)
CHUNK_BYTES = MAX_DATAGRAM - CHUNK_HEADER     # 158 bytes of page per datagram
NACK_ROOM = (MAX_DATAGRAM - 4) // 2           # indices per NACK datagram

FLAG_ZLIB = 1

KIND_SEARCH, KIND_ARTICLE, KIND_PAGE, KIND_UPLOAD = 1, 2, 3, 4
KIND_SPECS, KIND_SEARCH_ALL, KIND_PRINT = 5, 6, 7
KIND_NAMES = {KIND_SEARCH: "search", KIND_ARTICLE: "article",
              KIND_PAGE: "community page", KIND_UPLOAD: "upload",
              KIND_SPECS: "vault specs", KIND_SEARCH_ALL: "search", KIND_PRINT: "spore print"}
PRINT_OFFER = "spore-print"     # the address in an upload offer that carries a spore print

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


def build_uchunk(tid: int, index: int, payload: bytes) -> bytes:
    return struct.pack("<BHH", UCHUNK, tid, index) + payload


def build_unack(tid: int, missing: list[int]) -> bytes:
    chosen = missing[:NACK_ROOM]
    return (struct.pack("<BHB", UNACK, tid, len(chosen))
            + b"".join(struct.pack("<H", i) for i in chosen))


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


# ---------------------------------------------------------------- range test

def build_ping(pid: int, seq: int, flags: int = 0) -> bytes:
    """PING  17 | run id 2 | sequence 2 | flags 1"""
    return struct.pack("<BHHB", PING, pid, seq, flags)


def parse_ping(raw: bytes) -> tuple[int, int, int]:
    _, pid, seq, flags = struct.unpack("<BHHB", raw[:6])
    return pid, seq, flags


def _signal_byte(value, scale: float) -> int:
    if value is None:
        return SIGNAL_UNKNOWN
    return max(-128, min(126, round(float(value) * scale))) & 0xFF


def build_pong(pid: int, seq: int, snr: float | None, rssi: int | None, hops: int | None,
               flags: int, name: str = "") -> bytes:
    """PONG  18 | run id 2 | sequence 2 | snr×4 (int8) | rssi (int8) | hops 1 | flags 1 | name

    snr/rssi: how the Seed Vault heard the ping (127 = unknown). hops: the
    path length the ping arrived with (255: came by direct route, i.e. zero-hop)."""
    head = struct.pack("<BHHBBBB", PONG, pid, seq, _signal_byte(snr, 4), _signal_byte(rssi, 1),
                       255 if hops is None else hops & 0xFF, flags)
    body = name.encode()[:MAX_DATAGRAM - len(head)].decode(errors="ignore").encode()
    return head + body


def parse_pong(raw: bytes) -> dict:
    _, pid, seq, snr, rssi, hops, flags = struct.unpack("<BHHbbBB", raw[:9])
    return {"pid": pid, "seq": seq,
            "snr": None if snr == SIGNAL_UNKNOWN else snr / 4,
            "rssi": None if rssi == SIGNAL_UNKNOWN else rssi,
            "hops": hops, "flags": flags, "name": raw[9:].decode(errors="replace")}


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
