"""The client: ask for something over the radio, show it when it arrives.

    client = MossClient(transport, Cache())
    found  = await client.search("shrubbies band", on_progress=show)
    page   = await client.article(found.results[0].title, on_progress=show)

Both calls check the local cache first. A cache hit returns immediately and
uses no airtime; pass refresh=True to ask the gateway again.

Community pages:
    page   = await client.page("moss:<author id>/water-filters")   # verified Page
    result = await client.upload(page_file_bytes)                  # "stored v2"

on_progress receives a Progress snapshot whenever something changes and at
least twice a second, so a UI can keep a countdown moving even when no
datagram has arrived. That is what makes a minute-long wait feel like the
system working rather than hanging.
"""

from __future__ import annotations

import asyncio
import codecs
from pathlib import Path
import random
import time
import zlib
from dataclasses import dataclass, field
from struct import error as struct_error
from typing import Callable

from . import pages as PG
from . import protocol as P
from . import seal as S
from .cache import Cache, CachedArticle
from .protocol import SearchResult


class MossError(Exception):
    """Anything that stops a request: no gateway, refusal, corruption."""


class OlderVault(MossError):
    """The Seed Vault runs an older MOSS that doesn't know this request."""


def _explain(error: str, kind: int) -> str:
    """A vault from before a kind of request existed answers it with a bare
    'bad request'. Say what that actually means."""
    if error.strip() == "bad request" and kind >= P.KIND_PAGE:
        what = {P.KIND_PAGE: "community pages", P.KIND_UPLOAD: "uploads",
                P.KIND_SPECS: "vault specs", P.KIND_SEARCH_ALL: "community search",
                P.KIND_PRINT: "spore prints"}.get(kind, "this request")
        raise OlderVault(f"the Seed Vault is running an older MOSS that doesn't know about "
                         f"{what} yet. On the Pi: moss update (it restarts the vault), then try again.")
    return error


# ---------------------------------------------------------------- progress

STAGES = ("requesting", "queued", "fetching", "receiving", "repairing",
          "verifying", "uploading", "checking", "done")


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
    community: list[dict] = field(default_factory=list)   # {a, t, n, f, d, v}
    more: bool = False                                      # more community results on the vault


@dataclass
class PageOutcome:
    page: PG.Page
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
        self.status_at = time.monotonic()              # when the latest pulse changed
        self.bad_capsules = 0           # capsules for us that wouldn't open
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
                self.bad_capsules += 1  # not ours (same transfer number), or tampered with
                return
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
            if self.status != (state, value):
                self.status_at = time.monotonic()
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
                 request_attempts: int = 4, fetch_timeout: float = 120.0,
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
        self.vault_version: tuple[str, int] | None = None   # from the vault's HELLO
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
                if self._hello_result in ("trusted", "known"):
                    self.vault_version = S.hello_version(raw)
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

    async def upload(self, page: bytes, offer: str, on_progress: ProgressCallback | None = None,
                     gap: float = 1.0) -> tuple[bool, str]:
        """Send a page file to the Seed Vault. (stored?, the vault's message)"""
        if self.keyring is None:
            raise MossError("uploads must be encrypted; don't use --plain")
        async with self._lock:
            self._hello_result = ""
            self._other_vault = ""
            return await _upload(self, page, offer, on_progress or (lambda p: None), gap)

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
                raise MossError(_explain(ex.error, kind))

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
                    last_status_at = ex.status_at   # count down from the pulse itself
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


# ---------------------------------------------------------------- uploads

class _UploadExchange:
    """An upload in flight: we send the spores, the Seed Vault asks for
    what it's missing (UNACK) and finally says whether it kept the page."""

    def __init__(self, rid: int, seal: "S.ReaderSession"):
        self.rid, self.seal = rid, seal
        self.status: tuple[int, int] | None = None
        self.error: str | None = None
        self.accepted: dict | None = None
        self.reaccepted = False          # the vault sent its acceptance again: it has nothing
        self.missing: set[int] = set()
        self.last_unack = 0.0
        self.result: tuple[bool, str] | None = None
        self.accept_raw: bytes | None = None   # to learn the route back to the vault
        self.last_heard = time.monotonic()
        self.changed = asyncio.Event()

    def on_datagram(self, raw: bytes) -> None:
        if len(raw) < 3 or P.peek_tid(raw) != self.rid:
            return
        kind = raw[0]
        try:
            if kind == S.SACCEPT:
                if self.accepted is None:
                    self.accepted = self.seal.open_accept(raw)
                    self.accept_raw = raw
                else:
                    self.reaccepted = True
            elif kind == P.UNACK and self.accepted is not None:
                self.missing.update(i for i in P.parse_nack(raw)[1] if i < self.accepted["total"])
                self.last_unack = time.monotonic()
            elif kind == S.SRESULT and self.accepted is not None:
                self.result = self.seal.open_result(raw)
            elif kind == S.SERROR:
                self.error = self.seal.open_error(raw) or "the Seed Vault refused the upload"
            elif kind == P.STATUS:
                _, state, value = P.parse_status(raw)
                self.status = (state, value)
            else:
                return
        except (S.SealError, ValueError, IndexError, struct_error):
            return                       # not really from our vault, or damaged
        self.last_heard = time.monotonic()
        self.changed.set()


async def _upload(req: "Requester", page: bytes, offer: str, report: ProgressCallback,
                  gap: float, settle: float = 0.8, max_pokes: int = 8) -> tuple[bool, str]:
    rid = random.randint(1, 0xFFFF)
    seal = S.ReaderSession(await req._find_vault(report), rid, P.KIND_UPLOAD, offer)
    ex = _UploadExchange(rid, seal)
    req._active = ex
    started = time.monotonic()
    try:
        attempts, last_request = 0, 0.0
        chunks: list[bytes] = []
        sent_once = False
        pokes = 0
        poke_after = max(req.quiet * 2.5, 3 * gap + 2)

        def progress(stage: str, message: str = "") -> None:
            have = [i for i in range(len(chunks)) if i not in ex.missing] if sent_once else []
            report(Progress(stage=stage, message=message, elapsed=time.monotonic() - started,
                            total=len(chunks), have=have, attempt=attempts))

        rounds = {"n": 0}

        async def send(indexes: list[int], stage: str) -> None:
            rounds["n"] += 1
            route_of = getattr(req.tx, "return_path", None)
            route = route_of(ex.accept_raw) if route_of and ex.accept_raw and rounds["n"] <= 2 else None
            for n, i in enumerate(indexes):
                if ex.result or ex.error:
                    return
                if route is None:
                    await req.tx.send(P.build_uchunk(rid, i, chunks[i]))
                else:
                    await req.tx.send(P.build_uchunk(rid, i, chunks[i]), path=route)
                ex.missing.discard(i)
                if stage == "uploading":
                    report(Progress(stage=stage, elapsed=time.monotonic() - started,
                                    total=len(chunks), have=list(range(n + 1)), attempt=attempts))
                await asyncio.sleep(gap)
            ex.last_heard = time.monotonic()

        while True:
            now = time.monotonic()
            if ex.error:
                raise MossError(_explain(ex.error, P.KIND_UPLOAD))
            if ex.result:
                return ex.result
            if ex.accepted is None:
                waiting = ex.status is not None
                due = req.request_timeout if not waiting else max(req.request_timeout, poke_after)
                if attempts == 0 or (now - max(last_request, ex.last_heard) >= due):
                    # Offers are tiny and a refusal is the answer we need, so
                    # try a few more times than a read would before giving up.
                    if attempts >= req.request_attempts + (9 if waiting else 3):
                        raise MossError("no pulse from the Seed Vault. Is it running, "
                                        "and are you on the same channel?")
                    await req.tx.send(seal.request)
                    attempts += 1
                    last_request = time.monotonic()
                progress("queued" if waiting and ex.status[0] == P.ST_QUEUED else "requesting")
            elif not sent_once:
                chunks = seal.seal_upload(page)
                sent_once = True
                ex.reaccepted = False
                await send(list(range(len(chunks))), "uploading")
            elif ex.reaccepted:
                ex.reaccepted = False                # it heard none of them: everything again
                await send(list(range(len(chunks))), "repairing")
            elif ex.missing and now - ex.last_unack >= settle:
                await send(sorted(ex.missing), "repairing")
            elif now - ex.last_heard >= poke_after:
                pokes += 1
                if pokes > max_pokes:
                    raise MossError("the Seed Vault stopped answering in the middle of the upload")
                await req.tx.send(seal.request)      # "where are we?": it answers with what's missing
                ex.last_heard = time.monotonic()
            else:
                progress("checking" if not ex.missing else "repairing")
            ex.changed.clear()
            try:
                await asyncio.wait_for(ex.changed.wait(), 0.25)
            except asyncio.TimeoutError:
                pass
    finally:
        req._active = None


# ---------------------------------------------------------------- the client

class MossClient:
    def __init__(self, tx, cache: Cache | None = None, keyring: "S.Keyring | None" = None,
                 notice: Callable[[str], None] | None = None, home=None,
                 upload_gap: float = 1.0, **timing):
        """keyring: encrypt everything to the trusted Seed Vault (the normal
        case). Without one, requests and pages travel unencrypted.
        home: where seen-pages.json lives (default ~/.moss)."""
        self.cache = cache or Cache()
        self.requester = Requester(tx, keyring=keyring, notice=notice, **timing)
        self.seen = PG.SeenVersions(home)
        self.upload_gap = upload_gap
        from .config import moss_home
        from .social import Social
        from .sporeprint import CarriedPrints
        self.home = Path(home) if home else moss_home()
        self.social = Social(self.home)
        self.prints = CarriedPrints(self.home)
        self.share_prints = True             # pass spore prints on to vaults that have none

    async def search(self, query: str, refresh: bool = False,
                     on_progress: ProgressCallback | None = None) -> SearchOutcome:
        query = query.strip()
        if not query:
            raise MossError("empty search")
        if not refresh and (hit := self.cache.get_search(query)):
            results, at = hit
            if on_progress:
                on_progress(Progress(stage="done", message="from cache"))
            return SearchOutcome(query, results, True, at,
                                 self.rank(self.cache.get_community(query)))
        community, more = [], False
        try:
            got = await self.requester.fetch(P.KIND_SEARCH_ALL, f"0\x1f{query}", on_progress)
            data = _json(got.content)
            results = [SearchResult(t, b) for t, b in data.get("wiki", [])]
            community, more = data.get("community", []), bool(data.get("more"))
        except OlderVault:
            got = await self.requester.fetch(P.KIND_SEARCH, query, on_progress)   # Wikipedia only
            results = P.decode_results(got.content)
        community = self.rank(community)
        self.cache.put_search(query, results, community)
        if on_progress:
            on_progress(Progress(stage="done", elapsed=got.elapsed,
                                 total=got.chunks, have=list(range(got.chunks))))
        return SearchOutcome(query, results, False, time.time(), community, more)

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

    # ------------------------------------------------------------ community pages
    def _page_file(self, address: str) -> Path:
        aid, name = PG.parse_address(address)
        return Path(self.cache.folder) / "pages" / aid / f"{name}.mosspage"

    async def page(self, address: str, refresh: bool = False,
                   on_progress: ProgressCallback | None = None) -> PageOutcome:
        """Fetch a community page by its address, check its author's
        signature, and refuse an older version than one already seen."""
        address = address.strip()
        try:
            PG.parse_address(address)
        except PG.PageError as exc:
            raise MossError(str(exc)) from exc
        path = self._page_file(address)
        if not refresh and path.exists():
            try:
                page = PG.load(path.read_bytes())
                if on_progress:
                    on_progress(Progress(stage="done", message="from cache"))
                return PageOutcome(page, True, path.stat().st_mtime)
            except PG.PageError:
                pass                                   # damaged copy: fetch it again
        got = await self.requester.fetch(P.KIND_PAGE, address, on_progress)
        try:
            page = PG.load(got.content)
        except PG.PageError as exc:
            raise MossError(f"page came through blighted: {exc}. Not keeping it.") from exc
        if page.address != address:
            raise MossError("the Seed Vault sent a different page than the one asked for")
        try:
            self.seen.accept(page)
        except PG.PageError as exc:
            raise MossError(str(exc)) from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(got.content)
        if on_progress:
            on_progress(Progress(stage="done", elapsed=got.elapsed,
                                 total=got.chunks, have=list(range(got.chunks))))
        return PageOutcome(page, False, time.time())

    async def upload(self, data: bytes, on_progress: ProgressCallback | None = None) -> str:
        """Upload a signed page file. Returns the vault's message ("stored v2");
        raises MossError with the vault's reason if it wasn't kept."""
        try:
            page = PG.load(data)
        except PG.PageError as exc:
            raise MossError(f"not uploading a broken page file: {exc}") from exc
        offer = f"{page.address} {page.version} {len(data)}"
        try:
            stored, message = await self.requester.upload(data, offer, on_progress, self.upload_gap)
        except MossError as exc:
            if str(exc) == f"already has v{page.version} of this page":
                return "already on this Seed Vault"
            raise
        if not stored:
            raise MossError(message)
        return message

    # ------------------------------------------------------------ finding community pages
    def _saved_texts(self) -> list[str]:
        """Text of community pages saved here (to find what they link to)."""
        out = []
        folder = Path(self.cache.folder) / "pages"
        for f in folder.glob("*/*.mosspage") if folder.is_dir() else []:
            try:
                page = PG.load(f.read_bytes())
                if not page.deleted:
                    out.append(page.text())
            except (OSError, PG.PageError):
                continue
        return out

    def rank(self, community: list[dict]) -> list[dict]:
        """Re-sort the vault's results by what you follow and save (on this device only)."""
        return self.social.rerank(community, self._saved_texts()) if community else []

    async def more_community(self, query: str, offset: int,
                             on_progress: ProgressCallback | None = None) -> tuple[list[dict], bool]:
        got = await self.requester.fetch(P.KIND_SEARCH_ALL, f"{offset}\x1f{query}", on_progress)
        data = _json(got.content)
        return self.rank(data.get("community", [])), bool(data.get("more"))

    async def author_pages(self, author_id: str, offset: int = 0,
                           on_progress: ProgressCallback | None = None) -> tuple[list[dict], bool]:
        """An author's public pages on this vault, newest first."""
        try:
            got = await self.requester.fetch(P.KIND_SEARCH_ALL, f"{offset}\x1fauthor:{author_id}",
                                             on_progress)
        except MossError as exc:
            if "nothing found" in str(exc):
                return [], False
            raise
        data = _json(got.content)
        return data.get("community", []), bool(data.get("more"))

    # ------------------------------------------------------------ the vault itself
    async def specs(self, on_progress: ProgressCallback | None = None) -> dict:
        """What the Seed Vault says about itself (all public), and trade spore
        prints: pick up its print, or offer one if it has none."""
        got = await self.requester.fetch(P.KIND_SPECS, "specs", on_progress)
        specs = _json(got.content)
        if self.share_prints:
            try:
                specs["print_exchange"] = await self._trade_prints(specs)
            except MossError as exc:
                specs["print_exchange"] = f"couldn't trade spore prints: {exc}"
        return specs

    async def _trade_prints(self, specs: dict) -> str:
        from . import sporeprint as SP
        theirs = specs.get("spore_print")
        if theirs:
            best = self.prints.best(theirs["origin"])
            if best is None or len(best["links"]) > theirs["hops"]:
                got = await self.requester.fetch(P.KIND_PRINT, "print")
                try:
                    sp = SP.from_bytes(got.content)
                except SP.PrintError as exc:
                    return f"its spore print didn't check out ({exc}); ignored"
                if SP.summary(sp)["origin"] != theirs["origin"]:
                    return "its spore print didn't match what it described; ignored"
                self.prints.carry(sp)
                return "picked up its spore print"
            return "already carrying that spore print"
        carried = self.prints.best()
        if carried is None:
            return "neither of us has a spore print yet"
        data = SP.to_bytes(carried)
        stored, message = await self.requester.upload(
            data, f"{P.PRINT_OFFER} 1 {len(data)}", None, self.upload_gap)
        return f"gave it a spore print: {message}" if stored else f"offered a spore print: {message}"


def _json(content: bytes) -> dict:
    import json
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MossError("the Seed Vault's answer didn't make sense") from exc
    if not isinstance(data, dict):
        raise MossError("the Seed Vault's answer didn't make sense")
    return data
