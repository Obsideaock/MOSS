"""The client: ask for something over the radio, show it when it arrives.

    client = MossClient(transport, Cache())
    found  = await client.search("shrubbies band", on_progress=show)
    page   = await client.article(found.results[0].title, on_progress=show)

Both calls check the local cache first. A cache hit returns immediately and
uses no airtime; pass refresh=True to ask the gateway again.

on_progress receives a Progress snapshot whenever something changes and at
least twice a second, so a UI can keep a countdown moving even when no
datagram has arrived. That is what makes a minute-long wait feel like the
system working rather than hanging.
"""

from __future__ import annotations

import asyncio
import codecs
import random
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable

from . import protocol as P
from . import seal as S
from .cache import Cache, CachedArticle
from .protocol import SearchResult


class MossError(Exception):
    """Anything that stops a request: no gateway, refusal, corruption."""


# ---------------------------------------------------------------- progress

STAGES = ("requesting", "queued", "fetching", "receiving", "repairing",
          "verifying", "done")


@dataclass
class Progress:
    stage: str = "requesting"
    message: str = ""
    elapsed: float = 0.0
    total: int = 0                      # chunks expected (0 until the manifest)
    have: list[int] = field(default_factory=list)   # chunk indices received
    eta: float | None = None            # seconds, best current guess
    queue_position: int = 0
    attempt: int = 1                    # request transmissions so far
    preview: str = ""                   # text decoded so far (articles)

    @property
    def fraction(self) -> float:
        return len(self.have) / self.total if self.total else 0.0

    def to_dict(self) -> dict:
        return {"stage": self.stage, "message": self.message,
                "elapsed": round(self.elapsed, 1), "total": self.total,
                "have": self.have, "eta": None if self.eta is None else round(self.eta),
                "queue_position": self.queue_position, "attempt": self.attempt,
                "preview": self.preview}


ProgressCallback = Callable[[Progress], None]


@dataclass
class Fetched:
    name: str
    content: bytes
    elapsed: float
    chunks: int


@dataclass
class SearchOutcome:
    query: str
    results: list[SearchResult]
    from_cache: bool = False
    fetched_at: float = 0.0


@dataclass
class ArticleOutcome:
    title: str
    text: str
    from_cache: bool = False
    fetched_at: float = 0.0


# ---------------------------------------------------------------- one request

class _Exchange:
    """Everything the client knows about the request in flight."""

    def __init__(self, rid: int, want_preview: bool, seal: "S.ReaderSession | None" = None):
        self.rid = rid
        self.seal = seal                # None: an unencrypted (plain) request
        self.manifest: dict | None = None
        self.chunks: dict[int, bytes] = {}
        self.status: tuple[int, int] | None = None     # (state, value)
        self.error: str | None = None
        self.last_heard = time.monotonic()
        self.orphans = 0                # chunks heard for us since the last re-ask, no manifest yet
        self.early: dict[int, bytes] = {}   # those chunks, kept until the manifest arrives
        self.first_chunk_at: float | None = None
        self.changed = asyncio.Event()
        # Incremental decode of the in-order prefix, for the live preview.
        self.want_preview = want_preview
        self._next = 0
        self._zlib = None
        self._utf8 = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.preview = ""
        self._skip = None               # sealed: bytes of the title header still to skip

    def feed_preview(self) -> None:
        m = self.manifest
        if not (self.want_preview and m):
            return
        if self._zlib is None and m["flags"] & P.FLAG_ZLIB:
            self._zlib = zlib.decompressobj()
        while self._next in self.chunks:
            piece = self.chunks[self._next]
            offset = self._next * P.CHUNK_BYTES
            self._next += 1
            if self.seal is not None:
                # Decrypt early to show the text as it lands (not yet verified;
                # nothing is kept until the whole page passes its check).
                piece = self.seal.peek(piece, offset)[:max(0, m["body_len"] - offset)]
                if self._skip is None:
                    self._head = getattr(self, "_head", b"") + piece
                    if len(self._head) < 2:
                        continue
                    self._skip = 2 + int.from_bytes(self._head[:2], "little")
                    piece, self._head = self._head, b""
                if self._skip:
                    cut = min(self._skip, len(piece))
                    piece, self._skip = piece[cut:], self._skip - cut
            try:
                raw = self._zlib.decompress(piece) if self._zlib else piece
            except zlib.error:
                self.want_preview = False
                return
            self.preview += self._utf8.decode(raw)

    def on_datagram(self, raw: bytes) -> None:
        if len(raw) < 3:
            return
        kind, tid = raw[0], P.peek_tid(raw)
        if tid != self.rid:
            return                      # someone else's conversation
        self.last_heard = time.monotonic()
        if kind in (P.MANIFEST, S.SCAPSULE):
            if (kind == S.SCAPSULE) != (self.seal is not None):
                return                  # a reply of the other kind can't be for us
            if self.manifest is not None:
                self.changed.set()
                return
            try:
                m = self.seal.open_capsule(raw) if self.seal else P.parse_manifest(raw)
            except (S.SealError, ValueError, IndexError):
                return                  # not ours, or tampered with
            if self.manifest is None:
                self.manifest = m
                # Adopt what arrived while the manifest was missing.
                for index, payload in self.early.items():
                    if index < m["total"]:
                        self.chunks[index] = payload
                if self.chunks and self.first_chunk_at is None:
                    self.first_chunk_at = time.monotonic()
                self.early.clear()
                self.feed_preview()
        elif kind == P.CHUNK and not self.manifest:
            # The manifest was lost; keep the chunk and re-ask for the manifest.
            _, index, payload = P.parse_chunk(raw)
            self.early.setdefault(index, payload)
            self.orphans += 1
        elif kind == P.CHUNK:
            _, index, payload = P.parse_chunk(raw)
            if index < self.manifest["total"] and index not in self.chunks:
                if self.first_chunk_at is None:
                    self.first_chunk_at = time.monotonic()
                self.chunks[index] = payload
                self.feed_preview()
        elif kind == P.STATUS:
            _, state, value = P.parse_status(raw)
            self.status = (state, value)
        elif kind == P.ERROR and self.seal is None:
            self.error = P.parse_error(raw)[1] or "the Seed Vault refused the request"
        elif kind == S.SERROR and self.seal is not None:
            try:
                self.error = self.seal.open_error(raw) or "the Seed Vault refused the request"
            except S.SealError:
                return
        else:
            return
        self.changed.set()


class Requester:
    def __init__(self, tx, quiet: float = 6.0, request_timeout: float = 25.0,
                 request_attempts: int = 3, fetch_timeout: float = 120.0,
                 idle: float = 120.0, max_nacks: int = 14,
                 manifest_retry: float = 4.0, nack_spacing: float = 1.0,
                 keyring: "S.Keyring | None" = None,
                 notice: Callable[[str], None] | None = None):
        """quiet           seconds of silence before asking for missing chunks
        request_timeout  seconds before repeating an unanswered request
        manifest_retry   minimum seconds between re-asks when chunks arrive
                         but the manifest never did
        nack_spacing     pause between NACK datagrams of one long missing list
        keyring          the trusted Seed Vault; with one, every request and
                         reply is encrypted (None: plain, for tests and debugging)
        """
        self.tx = tx
        self.quiet, self.request_timeout = quiet, request_timeout
        self.request_attempts, self.fetch_timeout = request_attempts, fetch_timeout
        self.idle, self.max_nacks = idle, max_nacks
        self.manifest_retry, self.nack_spacing = manifest_retry, nack_spacing
        self._lock = asyncio.Lock()          # one conversation on the air at a time
        self._active: _Exchange | None = None
        self.keyring = keyring
        self.notice = notice or (lambda m: None)
        self._hello = asyncio.Event()
        self._hello_result = ""
        self._other_vault = ""
        tx.subscribe(self._on_datagram)

    def _on_datagram(self, raw: bytes) -> None:
        if raw and raw[0] == S.HELLO and self.keyring is not None:
            heard = S.verify_hello(raw)
            if heard:
                self._hello_result = self.keyring.offer(*heard)
                if self._hello_result in ("mismatch", "unexpected"):
                    # Another vault, or ours with new keys. Not a reason to stop:
                    # ours may answer too. It explains things only if nobody does.
                    self._other_vault = S.fingerprint(heard[0], heard[1])
                if self._hello_result == "trusted":
                    self.notice(f"Trusting Seed Vault {self.keyring.fingerprint} (first contact). "
                                f"Check it matches the fingerprint shown on the Pi.")
                self._hello.set()
            return
        if self._active is not None:
            self._active.on_datagram(raw)

    async def _find_vault(self, report: ProgressCallback) -> bytes:
        """The trusted vault's public key, asking for it over the air if
        this reader hasn't met the Seed Vault yet."""
        if self.keyring.x_pub:
            return self.keyring.x_pub
        started = time.monotonic()
        # First contact happens once per reader and the messages are tiny, so
        # it gets more tries: 8 makes it ~0.1% likely to fail at 25% loss.
        for attempt in range(1, self.request_attempts + 6):
            report(Progress(stage="requesting", message="looking for the Seed Vault",
                            elapsed=time.monotonic() - started, attempt=attempt))
            self._hello.clear()
            await self.tx.send(bytes([S.WHOIS]) + random.randint(1, 0xFFFF).to_bytes(2, "little"))
            try:
                await asyncio.wait_for(self._hello.wait(), self.request_timeout)
            except asyncio.TimeoutError:
                continue
            if self.keyring.x_pub:
                return self.keyring.x_pub
            if self._hello_result == "unexpected":
                raise MossError(f"a Seed Vault answered, but it isn't the one you pinned "
                                f"({self.keyring.fingerprint}). Not sending anything to it.")
        raise MossError("no pulse from the Seed Vault. Is it running, "
                        "and are you on the same channel?")

    async def fetch(self, kind: int, query: str,
                    on_progress: ProgressCallback | None = None) -> Fetched:
        async with self._lock:
            report = on_progress or (lambda p: None)
            self._hello_result = ""
            self._other_vault = ""
            rid = random.randint(1, 0xFFFF)
            seal = None
            if self.keyring is not None:
                seal = S.ReaderSession(await self._find_vault(report), rid, kind, query)
            ex = _Exchange(rid, kind == P.KIND_ARTICLE, seal)
            self._active = ex
            try:
                return await self._run(ex, kind, query, report)
            finally:
                self._active = None

    # ------------------------------------------------------------ the state machine

    async def _run(self, ex: _Exchange, kind: int, query: str,
                   report: ProgressCallback) -> Fetched:
        started = time.monotonic()
        attempts = 0
        last_request = 0.0
        last_status_at = started
        nacks = 0
        fruitless = 0                   # NACKs in a row that brought nothing new
        have_at_nack = 0
        gateway_eta: float | None = None
        last_report = 0.0
        last_sig = None
        request = ex.seal.request if ex.seal else P.build_request(ex.rid, kind, query)

        def snapshot(stage: str, message: str = "") -> Progress:
            m = ex.manifest
            have = sorted(ex.chunks)
            p = Progress(stage=stage, message=message,
                         elapsed=time.monotonic() - started,
                         total=m["total"] if m else 0, have=have,
                         attempt=attempts, preview=ex.preview)
            if ex.status and ex.status[0] == P.ST_QUEUED:
                p.queue_position = ex.status[1]
            if stage == "repairing":
                p.eta = None            # we can't know how long repairs take
            elif m and ex.first_chunk_at and len(have) >= 5:
                per = (time.monotonic() - ex.first_chunk_at) / (len(have) - 1)
                p.eta = max(0.0, (m["total"] - len(have)) * per)
            elif gateway_eta is not None:
                p.eta = max(0.0, gateway_eta - (time.monotonic() - last_status_at))
            return p

        while True:
            now = time.monotonic()
            m = ex.manifest

            if ex.error:
                raise MossError(ex.error)

            # Say it (again) until the gateway acknowledges.
            if m is None and ex.status is None:
                if now - last_request >= self.request_timeout or attempts == 0:
                    if attempts >= self.request_attempts:
                        if self._other_vault:
                            raise MossError(
                                f"your Seed Vault didn't answer, but one with a different key did "
                                f"({self._other_vault}; you trust {self.keyring.fingerprint}). If "
                                f"your Pi was reset on purpose, run: python -m moss trust --forget")
                        raise MossError("no pulse from the Seed Vault. Is it running, "
                                        "and are you on the same channel?")
                    await self.tx.send(request)
                    if attempts and self.keyring is not None:
                        # No answer: is the vault there at all, with the key we know?
                        await self.tx.send(bytes([S.WHOIS]) + ex.rid.to_bytes(2, "little"))
                    attempts += 1
                    last_request = time.monotonic()

            # The gateway answered but the page hasn't started.
            if m is None and ex.status is not None:
                state, value = ex.status
                if state == P.ST_SENDING:
                    gateway_eta = float(value)
                    last_status_at = ex.last_heard
                # Chunks are flying past but the manifest never reached us:
                # ask again; the gateway answers a repeat by re-sending it.
                # If the chunks have stopped and the re-sent manifest was lost
                # too, ask again once things go quiet; the gateway keeps
                # listening while we do.
                flowing = ex.orphans and now - last_request >= self.manifest_retry
                stalled = (ex.early and now - ex.last_heard >= self.quiet
                           and now - last_request >= self.quiet)
                # Nothing at all for a while (a lost error or capsule): ask again;
                # the vault repeats its last answer.
                silent = (now - ex.last_heard >= self.request_timeout
                          and now - last_request >= self.request_timeout)
                if flowing or stalled or silent:
                    await self.tx.send(request)
                    last_request = time.monotonic()
                    ex.orphans = 0
                if now - ex.last_heard > self.fetch_timeout:
                    raise MossError("the Seed Vault went quiet before the spores arrived")

            # Page in flight.
            if m is not None:
                if len(ex.chunks) == m["total"]:
                    report(snapshot("verifying"))
                    return self._finish(ex, started)
                if now - ex.last_heard > self.idle:
                    raise MossError(f"nothing heard for {self.idle:.0f}s; "
                                    f"got {len(ex.chunks)}/{m['total']} spores")
                if now - ex.last_heard >= self.quiet:
                    # Only give up on asks that go unanswered; a long article
                    # on a bad channel may need many asks that each make progress.
                    fruitless = 0 if len(ex.chunks) > have_at_nack else fruitless + 1
                    have_at_nack = len(ex.chunks)
                    if fruitless > self.max_nacks:
                        raise MossError(f"transfer incomplete: "
                                        f"{len(ex.chunks)}/{m['total']} spores")
                    await self._ask_for_gaps(ex)
                    nacks += 1
                    ex.last_heard = time.monotonic()

            # Pick the stage a UI should show.
            if m is not None:
                stage = "repairing" if nacks else "receiving"
            elif ex.status is None:
                stage = "requesting"
            elif ex.status[0] == P.ST_QUEUED:
                stage = "queued"
            elif ex.status[0] == P.ST_FETCHING:
                stage = "fetching"
            else:
                stage = "receiving"

            sig = (stage, len(ex.chunks), ex.status, attempts, m is not None)
            if sig != last_sig or now - last_report >= 0.5:
                report(snapshot(stage))
                last_sig, last_report = sig, now

            ex.changed.clear()
            try:
                await asyncio.wait_for(ex.changed.wait(), 0.25)
            except asyncio.TimeoutError:
                pass

    async def _ask_for_gaps(self, ex: _Exchange) -> None:
        m = ex.manifest
        missing = [i for i in range(m["total"]) if i not in ex.chunks]
        batches = [missing[i:i + P.NACK_ROOM]
                   for i in range(0, len(missing), P.NACK_ROOM)]
        for batch in batches:
            await self.tx.send(P.build_nack(ex.rid, batch))
            if len(batches) > 1:
                await asyncio.sleep(self.nack_spacing)

    def _finish(self, ex: _Exchange, started: float) -> Fetched:
        m = ex.manifest
        blob = b"".join(ex.chunks[i] for i in range(m["total"]))
        if ex.seal is not None:
            try:
                m["name"], blob = ex.seal.open_body(blob, m["body_len"])
            except S.SealError as exc:
                raise MossError("page came through blighted (failed its integrity check); "
                                "not keeping it") from exc
        try:
            content = zlib.decompress(blob) if m["flags"] & P.FLAG_ZLIB else blob
        except zlib.error as exc:
            raise MossError("page came through blighted (corrupted in transit); not keeping it") from exc
        if P.short_hash(content) != m["hash"]:
            raise MossError("page came through blighted (failed its hash check); not keeping it")
        # Tell the gateway it can stop listening. Sent twice: if the only DONE
        # is lost, the gateway sits out its whole wait and everyone queued
        # behind this request stalls with it.
        asyncio.ensure_future(self._say_done(ex.rid))
        return Fetched(m["name"], content, time.monotonic() - started, m["total"])

    async def _say_done(self, rid: int) -> None:
        try:
            await self.tx.send(P.build_done(rid))
            await asyncio.sleep(self.nack_spacing)
            await self.tx.send(P.build_done(rid))
        except Exception:
            pass                        # best effort; the gateway times out anyway


# ---------------------------------------------------------------- the client

class MossClient:
    def __init__(self, tx, cache: Cache | None = None, keyring: "S.Keyring | None" = None,
                 notice: Callable[[str], None] | None = None, **timing):
        """keyring: encrypt everything to the trusted Seed Vault (the normal
        case). Without one, requests and pages travel unencrypted."""
        self.cache = cache or Cache()
        self.requester = Requester(tx, keyring=keyring, notice=notice, **timing)

    async def search(self, query: str, refresh: bool = False,
                     on_progress: ProgressCallback | None = None) -> SearchOutcome:
        query = query.strip()
        if not query:
            raise MossError("empty search")
        if not refresh and (hit := self.cache.get_search(query)):
            results, at = hit
            if on_progress:
                on_progress(Progress(stage="done", message="from cache"))
            return SearchOutcome(query, results, True, at)
        got = await self.requester.fetch(P.KIND_SEARCH, query, on_progress)
        results = P.decode_results(got.content)
        self.cache.put_search(query, results)
        if on_progress:
            on_progress(Progress(stage="done", elapsed=got.elapsed,
                                 total=got.chunks, have=list(range(got.chunks))))
        return SearchOutcome(query, results, False, time.time())

    async def article(self, title: str, refresh: bool = False,
                      on_progress: ProgressCallback | None = None) -> ArticleOutcome:
        title = title.strip()
        if not title:
            raise MossError("no article title")
        if not refresh and (hit := self.cache.get_article(title)):
            meta, text = hit
            if on_progress:
                on_progress(Progress(stage="done", message="from cache"))
            return ArticleOutcome(meta.title, text, True, meta.fetched_at)
        got = await self.requester.fetch(P.KIND_ARTICLE, title, on_progress)
        text = got.content.decode("utf-8", errors="replace")
        meta = self.cache.put_article(got.name, text, aliases=[title])
        if on_progress:
            on_progress(Progress(stage="done", elapsed=got.elapsed,
                                 total=got.chunks, have=list(range(got.chunks)),
                                 preview=text))
        return ArticleOutcome(got.name, text, False, meta.fetched_at)

    def cached(self) -> list[CachedArticle]:
        return self.cache.list_articles()
