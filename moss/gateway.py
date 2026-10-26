"""The Seed Vault (the gateway): a radio plus a library.

It listens on the channel for REQUESTs, fetches the answer from its source
(Wikipedia, or a local folder), and sends it back using the same
manifest / chunk / NACK / DONE transfer page_transfer.py uses.

Requests are served one at a time, in order. Two people asking at once get
queue positions rather than two interleaved transfers fighting over the same
few kilobits per second.
"""

from __future__ import annotations

import asyncio
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable

from . import protocol as P
from . import seal as S
from .sources import SourceError

AIRTIME_PER_DATAGRAM = 0.55     # seconds, fast preset; used only for ETAs


@dataclass
class Session:
    rid: int
    kind: int
    query: str
    created: float = field(default_factory=time.monotonic)
    state: str = "queued"               # queued, fetching, sending, finished
    manifest: bytes | None = None
    chunks: list[bytes] = field(default_factory=list)
    reported: set[int] = field(default_factory=set)
    last_nack: float = 0.0
    heard: float = 0.0                  # last repeat request from the client
    done: bool = False
    paused: bool = False                # stopped waiting, but kept the chunks in case
                                        # the client's missing-list was only lost
    resume: list[int] | None = None     # chunks to resend when a paused session resumes
    seal: "S.VaultSession | None" = None  # encrypted request: reply encrypted too
    error_reply: bytes | None = None    # the error we answered with, to repeat if it was lost
    wake: asyncio.Event = field(default_factory=asyncio.Event)


class Gateway:
    def __init__(self, tx, source, gap: float = 1.0, rounds: int = 10,
                 wait: float = 30.0, settle: float = 1.6, max_queue: int = 6,
                 log: Callable[[str], None] = print, keys: "S.VaultKeys | None" = None,
                 pad_sizes: bool = False, log_queries: bool = False):
        """gap     seconds between datagrams (airtime pacing)
        wait    how long to listen for NACK/DONE after the last chunk; must be
                comfortably longer than the client's --quiet
        settle  after a NACK, keep listening this long for more NACK datagrams
                (a long missing list spans several); must exceed the client's
                spacing between them, and be well under the client's --quiet
        keys        the Seed Vault's identity; with it, encrypted requests are
                    answered with encrypted replies
        pad_sizes   round every encrypted page up to a standard spore count so
                    its size says less about what it is (~10% more airtime)
        log_queries write the titles of encrypted requests to the log (off:
                    the Pi keeps no list of what people read)
        """
        self.tx, self.source = tx, source
        self.gap, self.rounds, self.wait, self.settle = gap, rounds, wait, settle
        self.max_queue = max_queue
        self.log = log
        self.keys, self.pad_sizes, self.log_queries = keys, pad_sizes, log_queries
        self._last_hello = 0.0
        self._answered: list[bytes] = []            # recent WHOIS already answered
        self.sessions: dict[int, Session] = {}
        self.queue: asyncio.Queue[Session] = asyncio.Queue()
        self._worker: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.tx.subscribe(self._on_datagram)
        self._worker = asyncio.create_task(self._work())
        if self.keys is not None:
            await self._hello()                     # one short announcement at start

    async def _hello(self, asked_by: bytes | None = None) -> None:
        """Say who we are (public key + fingerprint). Every distinct WHOIS gets
        one answer; a repeated copy of the same WHOIS doesn't get a second."""
        if asked_by is not None:
            if asked_by in self._answered:
                return
            self._answered.append(asked_by)
            del self._answered[:-32]
        self._last_hello = time.monotonic()
        await self.tx.send(self.keys.hello())

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    async def run_forever(self) -> None:
        await self.start()
        await asyncio.Event().wait()

    # ------------------------------------------------------------ incoming

    async def _on_datagram(self, raw: bytes) -> None:
        if not raw:
            return
        kind = raw[0]
        try:
            if kind == P.REQUEST:
                await self._on_request(*P.parse_request(raw))
            elif kind == S.SREQUEST and self.keys is not None:
                try:
                    rid, k, query, sealed = self.keys.open_request(raw)
                except S.SealError:
                    return                          # meant for another vault, or garbage
                await self._on_request(rid, k, query, sealed)
            elif kind == S.WHOIS and self.keys is not None:
                await self._hello(asked_by=raw[:3])
            elif kind == P.NACK:
                rid, missing = P.parse_nack(raw)
                s = self.sessions.get(rid)
                if not s or s.done:
                    pass
                elif s.paused:
                    # We gave up listening, but the client is still asking:
                    # its earlier lists were lost in the air. Pick it back up.
                    s.paused = False
                    s.resume = sorted(set(missing))
                    s.state = "queued"
                    self.log(f"[{rid}] reader still asking for a bare patch of {len(missing)} spore(s), resuming")
                    await self.queue.put(s)
                elif s.state == "queued" and s.resume is not None:
                    s.resume = sorted(set(s.resume) | set(missing))
                elif s.state in ("sending", "finished"):
                    s.reported.update(missing)
                    s.last_nack = time.monotonic()
                    s.wake.set()
            elif kind == P.DONE:
                s = self.sessions.get(P.peek_tid(raw))
                if s:
                    s.done = True
                    s.wake.set()
        except Exception as exc:        # a garbled datagram must never kill the gateway
            self.log(f"  ignoring bad datagram: {exc!r}")

    def _error(self, rid: int, message: str, seal: "S.VaultSession | None") -> bytes:
        """Errors can name what was asked for ("no such article: ..."), so an
        encrypted request gets an encrypted error."""
        return seal.error(message) if seal else P.build_error(rid, message)

    def _what(self, s: Session) -> str:
        if s.seal is not None and not self.log_queries:
            return f"{P.KIND_NAMES[s.kind]} (encrypted; not logged)"
        return f"{P.KIND_NAMES[s.kind]}: {s.query!r}"

    async def _on_request(self, rid: int, kind: int, query: str,
                          seal: "S.VaultSession | None" = None) -> None:
        if kind not in P.KIND_NAMES or not query.strip():
            await self.tx.send(self._error(rid, "bad request", seal))
            return

        existing = self.sessions.get(rid)
        if existing:
            # The client didn't hear us. Say it again rather than start over.
            self.log(f"  [{rid}] repeated request, re-sending the capsule")
            existing.heard = time.monotonic()      # the client is still there
            existing.wake.set()
            if existing.error_reply:
                await self.tx.send(existing.error_reply)
            elif existing.manifest:
                await self.tx.send(existing.manifest)
            else:
                await self.tx.send(self._status_for(existing))
            return

        self._purge()
        if self.queue.qsize() >= self.max_queue:
            self.log(f"  [{rid}] refused, queue full")
            await self.tx.send(self._error(rid, "Seed Vault busy, try again soon", seal))
            return

        session = Session(rid, kind, query.strip(), seal=seal)
        self.sessions[rid] = session
        self.log(f"[{rid}] {self._what(session)}")
        await self.queue.put(session)
        await self.tx.send(self._status_for(session))

    def _status_for(self, s: Session) -> bytes:
        if s.state == "queued":
            ahead = list(self.queue._queue).index(s) if s in self.queue._queue else 0
            return P.build_status(s.rid, P.ST_QUEUED, ahead)
        return P.build_status(s.rid, P.ST_FETCHING)

    def _purge(self, older_than: float = 900.0) -> None:
        now = time.monotonic()
        for rid in [r for r, s in self.sessions.items()
                    if s.state == "finished" and now - s.created > older_than]:
            del self.sessions[rid]

    # ------------------------------------------------------------ worker

    async def _work(self) -> None:
        while True:
            session = await self.queue.get()
            try:
                await self._serve(session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"[{session.rid}] failed: {exc!r}")
                session.error_reply = self._error(session.rid, "Seed Vault error", session.seal)
                await self.tx.send(session.error_reply)
            finally:
                session.state = "finished"

    async def _serve(self, s: Session) -> None:
        if s.resume is not None:
            to_send, s.resume = s.resume, None
            s.state = "sending"
            await self._transmit(s, [i for i in to_send if i < len(s.chunks)])
            return
        s.state = "fetching"
        await self.tx.send(P.build_status(s.rid, P.ST_FETCHING))
        started = time.monotonic()
        try:
            if s.kind == P.KIND_SEARCH:
                results = await self.source.search(s.query)
                if not results:
                    raise SourceError("nothing found")
                name, content = "results", P.encode_results(results)
            else:
                title, text = await self.source.article(s.query)
                name, content = title, text.encode()
        except SourceError as exc:
            self.log(f"[{s.rid}] " + (str(exc) if s.seal is None or self.log_queries
                                       else "not found (encrypted; not logged)"))
            s.error_reply = self._error(s.rid, str(exc), s.seal)
            await self.tx.send(s.error_reply)
            return
        self.log(f"[{s.rid}] fetched {len(content)} bytes in "
                 f"{time.monotonic() - started:.1f}s")

        blob, flags = content, 0
        squeezed = zlib.compress(content, 9)
        if len(squeezed) < len(content):
            blob, flags = squeezed, P.FLAG_ZLIB
        if s.seal is not None:
            s.manifest, s.chunks = s.seal.seal_reply(name, blob, flags, P.short_hash(content),
                                                     self.pad_sizes)
        else:
            s.chunks = [blob[i:i + P.CHUNK_BYTES]
                        for i in range(0, len(blob), P.CHUNK_BYTES)] or [b""]
            s.manifest = P.build_manifest(s.rid, len(s.chunks), len(blob),
                                          P.short_hash(content), name, flags)
        s.state = "sending"

        eta = int(len(s.chunks) * (self.gap + AIRTIME_PER_DATAGRAM))
        self.log(f"[{s.rid}] releasing {len(s.chunks)} spore{'' if len(s.chunks) == 1 else 's'} "
                 f"({len(content)} -> {len(blob)} bytes{', encrypted' if s.seal else ''}), about {eta}s")
        await self.tx.send(P.build_status(s.rid, P.ST_SENDING, eta))
        await asyncio.sleep(self.gap)
        await self.tx.send(s.manifest)
        await asyncio.sleep(self.gap)

        await self._transmit(s, list(range(len(s.chunks))))

    async def _transmit(self, s: Session, to_send: list[int]) -> None:
        """Send these chunks, then repair from the client's NACKs until it says
        DONE, goes quiet, or we run out of rounds."""
        for rnd in range(1, self.rounds + 1):
            for index in to_send:
                await self.tx.send(P.build_chunk(s.rid, index, s.chunks[index]))
                await asyncio.sleep(self.gap)

            s.reported.clear()
            round_end = time.monotonic()
            # Listen for NACK/DONE. A repeat request (the client lost the
            # manifest) restarts the wait rather than letting it run out.
            while not s.done and not s.reported:
                left = max(round_end, s.heard) + self.wait - time.monotonic()
                if left <= 0:
                    break
                s.wake.clear()
                try:
                    await asyncio.wait_for(s.wake.wait(), left)
                except asyncio.TimeoutError:
                    break
            if s.done:
                self.log(f"[{s.rid}] delivered")
                return
            if not s.reported:
                self.log(f"[{s.rid}] no reply from the reader, pausing")
                s.paused = True
                return

            # A long missing list arrives as several NACK datagrams; wait for
            # the stragglers and take the union.
            while time.monotonic() - s.last_nack < self.settle and not s.done:
                await asyncio.sleep(min(0.2, self.settle / 4))
            if s.done:
                self.log(f"[{s.rid}] delivered")
                return
            to_send = sorted(i for i in s.reported if i < len(s.chunks))
            if not to_send:
                return
            self.log(f"[{s.rid}] repair {rnd}: bare patch of {len(to_send)} spore(s), resending")
        self.log(f"[{s.rid}] paused after {self.rounds} rounds")
        s.paused = True
