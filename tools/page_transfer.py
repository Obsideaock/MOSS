"""Send a whole page across the mesh, chunk by chunk, and verify it arrived.

One publisher, one subscriber, one file. The content is hashed before it
leaves and checked after it lands, so a corrupted or incomplete page is
rejected rather than written.

    # receiver first — it waits for a manifest
    python page_transfer.py --port COM12 --channel 1 --recv

    # then the sender
    python page_transfer.py --port COM16 --channel 1 --send sample_page.md

Compression is on by default (--no-zlib turns it off); if a file doesn't
compress, the raw bytes are sent instead.

On the wire, all inside 163-byte datagrams:

    MANIFEST   what's coming: chunk count, length, content hash, name
    READY      receiver has nothing yet, send everything
    NACK       receiver's list of chunk numbers it still needs
    CHUNK      one numbered slice of the (optionally compressed) bytes
    ROUND_END  sender has finished a pass; report gaps now
    DONE       receiver has it all and the hash matched

EITHER SIDE CAN DIE AND THE TRANSFER STILL RESUMES. The receiver keeps what
it has in a .moss-partial file. The manifest is a question, not an
announcement: the sender asks "who needs what?" and waits for the answer
before spending airtime, so a sender that lost power picks up where the
receiver actually is instead of starting from zero.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import struct
import time
import zlib
from pathlib import Path, PurePosixPath

from meshcore import MeshCore, EventType
from serial.tools import list_ports

MOSS_DATA_TYPE = 0xFF01
MAX_DATAGRAM = 163

MANIFEST, CHUNK, NACK, DONE, ROUND_END, READY = 1, 2, 3, 4, 5, 6
CHUNK_HEADER = 5                               # type(1) + tid(2) + index(2)
CHUNK_BYTES = MAX_DATAGRAM - CHUNK_HEADER      # 158 bytes of page per datagram
NACK_HEADER = 5                                # type(1) + tid(2) + count(1) + last(1)
NACK_ROOM = (MAX_DATAGRAM - NACK_HEADER) // 2  # 79 indices per datagram

FLAG_ZLIB = 1


def short_hash(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()[:8]


# ---------------------------------------------------------------- packets

def build_manifest(tid: int, total: int, length: int, digest: bytes,
                   name: str, flags: int) -> bytes:
    name_bytes = name.encode()[:100]
    return (struct.pack("<BHHIB", MANIFEST, tid, total, length, flags)
            + digest + bytes([len(name_bytes)]) + name_bytes)


def parse_manifest(raw: bytes) -> dict:
    _, tid, total, length, flags = struct.unpack("<BHHIB", raw[:10])
    name_len = raw[18]
    return {"tid": tid, "total": total, "length": length, "flags": flags,
            "hash": raw[10:18],
            "name": raw[19:19 + name_len].decode(errors="replace")}


def build_chunk(tid: int, index: int, payload: bytes) -> bytes:
    return struct.pack("<BHH", CHUNK, tid, index) + payload


def build_nack(tid: int, missing: list[int], last: bool) -> bytes:
    """One batch of missing indices. `last` says whether more follow."""
    chosen = missing[:NACK_ROOM]
    return (struct.pack("<BHBB", NACK, tid, len(chosen), int(last))
            + b"".join(struct.pack("<H", i) for i in chosen))


def parse_nack(raw: bytes) -> tuple[int, list[int], bool]:
    _, tid, count, last = struct.unpack("<BHBB", raw[:NACK_HEADER])
    indices = [struct.unpack("<H", raw[NACK_HEADER + 2 * i:NACK_HEADER + 2 + 2 * i])[0]
               for i in range(count)]
    return tid, indices, bool(last)


def nack_batches(tid: int, missing: list[int]) -> list[bytes]:
    groups = [missing[i:i + NACK_ROOM] for i in range(0, len(missing), NACK_ROOM)]
    return [build_nack(tid, g, last=(n == len(groups) - 1))
            for n, g in enumerate(groups)] or [build_nack(tid, [], last=True)]


def build_small(kind: int, tid: int) -> bytes:
    return struct.pack("<BH", kind, tid)


# ---------------------------------------------------------------- progress

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


def fmt_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


class Progress:
    """tqdm when it's installed, a plain one-line counter when it isn't."""

    def __init__(self, total: int, label: str, quiet: bool = False):
        self.total, self.label, self.n = total, label, 0
        self.start = time.monotonic()
        self.quiet = quiet
        self.bar = None
        if tqdm and not quiet:
            self.bar = tqdm(total=total, desc=label, unit="chunk",
                            bar_format="{desc} {percentage:3.0f}%|{bar}| "
                                       "{n_fmt}/{total_fmt} [{elapsed}<{remaining}]")

    def update(self, n: int = 1, note: str = "") -> None:
        self.n += n
        if self.bar:
            if note:
                self.bar.set_postfix_str(note, refresh=False)
            self.bar.update(n)
            return
        if self.quiet:
            return
        elapsed = time.monotonic() - self.start
        rate = self.n / elapsed if elapsed else 0
        left = (self.total - self.n) / rate if rate else 0
        pct = self.n / self.total * 100 if self.total else 100
        print(f"\r  {self.label} {pct:5.1f}%  {self.n}/{self.total}  "
              f"{fmt_time(left)} left {note}   ", end="", flush=True)

    def write(self, text: str) -> None:
        if self.bar:
            self.bar.write(text)
        else:
            print(f"\n{text}")

    def close(self) -> None:
        if self.bar:
            self.bar.close()
        elif not self.quiet:
            print()


# ---------------------------------------------------------------- plumbing

def pick_port() -> str:
    ports = [p for p in list_ports.comports()
             if "bluetooth" not in p.description.lower()]
    if not ports:
        raise SystemExit("no serial ports found")
    if len(ports) == 1:
        print(f"using {ports[0].device} ({ports[0].description})")
        return ports[0].device
    for p in ports:
        print(f"  {p.device:20} {p.description}")
    raise SystemExit("pick one with --port")


async def open_radio(port: str, baud: int, attempts: int = 6):
    last = None
    for n in range(1, attempts + 1):
        try:
            mc = await MeshCore.create_serial(port, baud)
            if n > 1:
                print(f"connected on attempt {n}")
            return mc
        except Exception as exc:
            last = exc
            text = str(exc).lower()
            if not any(k in text for k in ("access is denied", "permission",
                                           "could not open port", "in use")):
                raise
            print(f"  {port} busy (attempt {n}/{attempts}), waiting...")
            await asyncio.sleep(1.5)
    raise SystemExit(f"{port} stayed busy. Close other terminals, stray "
                     f"python.exe, or the MeshCore client. Last error: {last}")


def payload_bytes(event, data_type: int) -> bytes | None:
    """Pull our datagram out of an event, or None if it isn't ours."""
    p = event.payload if isinstance(event.payload, dict) else {}
    if p.get("data_type") != data_type:
        return None
    raw = p.get("payload")
    if isinstance(raw, str):
        try:
            return bytes.fromhex(raw)
        except ValueError:
            return None
    return bytes(raw) if isinstance(raw, (bytes, bytearray)) else None


async def start_polling(mc, interval: float = 1.0, on_idle=None) -> asyncio.Task:
    """The radio queues messages and does NOT reliably push binary datagrams,
    so ask for them continuously. (start_auto_message_fetching only wakes on
    MESSAGES_WAITING, which the firmware raises for chat but not for these.)

    `on_idle` is called whenever the radio says its queue is empty. That is
    the only moment we actually know we're caught up with the air: datagrams
    can sit queued on the radio for a second or more before we see them, so
    without it a receiver will declare chunks missing that it already has.
    """
    async def loop() -> None:
        while True:
            try:
                result = await mc.commands.get_msg()
                if getattr(result, "type", None) in (EventType.NO_MORE_MSGS,
                                                     EventType.ERROR):
                    if on_idle:
                        on_idle()
                    await asyncio.sleep(interval)
                else:
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"  (poll error: {exc})")
                await asyncio.sleep(interval)
    return asyncio.create_task(loop())


# ---------------------------------------------------------------- resuming

def safe_name(name: str, folder: Path) -> Path:
    """The sender's filename is untrusted: strip it to a bare name."""
    base = PurePosixPath(name.replace("\\", "/")).name
    base = "".join(c for c in base if c.isalnum() or c in "._- ").strip()
    while base.startswith("."):
        base = base[1:]
    return folder / (base or "received_page")


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(1, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path


def resolve_out(out: Path | None, sender_name: str, folder: Path) -> Path:
    """--out with no extension borrows the sender's."""
    sender_suffix = Path(PurePosixPath(sender_name.replace("\\", "/")).name).suffix
    if out is None:
        return safe_name(sender_name, folder)
    if out.suffix:
        return out
    return out.with_suffix(sender_suffix) if sender_suffix else out


def partial_path(out: Path) -> Path:
    return out.with_suffix(out.suffix + ".moss-partial")


def save_partial(out: Path, manifest: dict, chunks: dict[int, bytes]) -> Path:
    record = {
        "name": manifest["name"], "hash": manifest["hash"].hex(),
        "flags": manifest["flags"], "total": manifest["total"],
        "length": manifest["length"],
        "chunks": {str(i): c.hex() for i, c in chunks.items()},
    }
    path = partial_path(out)
    path.write_text(json.dumps(record))
    return path


def load_partial(out: Path, manifest: dict) -> dict[int, bytes]:
    """Reload chunks saved earlier, for this exact content AND encoding.

    The hash covers the UNCOMPRESSED content, so it alone doesn't prove the
    chunks match: a --zlib send and a raw send of one file share a hash.
    Flags and length have to match too, or compressed and raw chunks could
    be stitched together into garbage.
    """
    path = partial_path(out)
    if not path.exists():
        return {}
    try:
        record = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    if (record.get("hash") != manifest["hash"].hex()
            or record.get("total") != manifest["total"]
            or record.get("flags") != manifest["flags"]
            or record.get("length") != manifest["length"]):
        return {}
    try:
        return {int(i): bytes.fromhex(h) for i, h in record.get("chunks", {}).items()}
    except ValueError:
        return {}


# ---------------------------------------------------------------- sending

def pack(content: bytes, use_zlib: bool) -> tuple[bytes, int]:
    """Compress only when it actually helps — a JPEG gets bigger, not smaller."""
    if not use_zlib:
        return content, 0
    squeezed = zlib.compress(content, 9)
    if len(squeezed) < len(content):
        return squeezed, FLAG_ZLIB
    return content, 0


def estimate_seconds(chunk_count: int, gap: float) -> float:
    """The radio accepts a datagram in ~20ms and transmits while we wait out
    the gap, so airtime overlaps the gap rather than adding to it."""
    return chunk_count * gap


async def send_page(mc, channel: int, path: Path, gap: float, rounds: int,
                    data_type: int, use_zlib: bool, wait: float,
                    handshake: float = 4.0) -> None:
    content = path.read_bytes()
    digest = short_hash(content)
    blob, flags = pack(content, use_zlib)
    chunks = [blob[i:i + CHUNK_BYTES] for i in range(0, len(blob), CHUNK_BYTES)] or [b""]
    tid = random.randint(1, 0xFFFF)

    if flags & FLAG_ZLIB:
        note = f" -> {len(blob)} compressed ({len(blob) / len(content):.0%})"
    elif use_zlib:
        note = " (compression didn't help, sending raw)"
    else:
        note = ""
    print(f"\n{path.name}: {len(content)} bytes{note}")
    print(f"{len(chunks)} chunks of up to {CHUNK_BYTES} bytes, transfer id {tid}, "
          f"hash {digest.hex()}")

    reported: set[int] = set()
    reply = {"any": False, "done": False, "ready": False, "last_batch": False}

    async def on_data(event) -> None:
        raw = payload_bytes(event, data_type)
        if not raw:
            return
        kind = raw[0]
        if kind == DONE and struct.unpack("<H", raw[1:3])[0] == tid:
            reply["any"] = reply["done"] = True
        elif kind == READY and struct.unpack("<H", raw[1:3])[0] == tid:
            reply["any"] = reply["ready"] = reply["last_batch"] = True
        elif kind == NACK:
            ntid, missing, last = parse_nack(raw)
            if ntid == tid:
                reply["any"] = True
                reported.update(missing)
                if last:
                    reply["last_batch"] = True

    mc.subscribe(EventType.CHANNEL_DATA_RECV, on_data)
    poller = await start_polling(mc)

    async def collect(deadline: float) -> None:
        """Wait for a reply, then briefly for the rest of its batches."""
        while time.monotonic() < deadline and not reply["any"]:
            await asyncio.sleep(0.2)
        if not reply["any"]:
            return
        tail = time.monotonic() + 4.0
        while time.monotonic() < tail and not (reply["last_batch"] or reply["done"]):
            await asyncio.sleep(0.2)

    try:
        # ---- Handshake: ask who needs what before spending any airtime.
        # This is what lets a sender that lost power resume mid-transfer.
        manifest = build_manifest(tid, len(chunks), len(blob), digest,
                                  path.name, flags)
        to_send = list(range(len(chunks)))
        for attempt in range(1, 4):
            reported.clear()
            reply.update(any=False, done=False, ready=False, last_batch=False)
            await mc.commands.send_channel_data(channel, data_type, manifest)
            print(f"  manifest sent (attempt {attempt}), waiting "
                  f"{handshake:.0f}s for the receiver to say what it needs...")
            await collect(time.monotonic() + handshake)
            if reply["done"]:
                print("  receiver already has this page — nothing to send")
                return
            blind = 0
            if reply["ready"]:
                print(f"  receiver is starting fresh: sending all {len(chunks)}")
                break
            if reply["any"]:
                to_send = sorted(i for i in reported if i < len(chunks))
                print(f"  receiver already holds {len(chunks) - len(to_send)} "
                      f"chunk(s): sending the remaining {len(to_send)}")
                break
        else:
            print("  no answer — sending everything blind "
                  "(is the receiver running?)")

        print(f"  estimated time: {fmt_time(estimate_seconds(len(to_send), gap))}\n")
        blind = 0          # consecutive rounds sent with no reply at all

        for rnd in range(1, rounds + 1):
            if not to_send:
                break
            bar = Progress(len(to_send),
                           "sending" if rnd == 1 else f"repair {rnd - 1}")
            next_at = time.monotonic()
            for index in to_send:
                if reply["done"]:
                    break
                # Schedule from the START of each send, so the serial
                # round-trip isn't added on top of the gap every time.
                await asyncio.sleep(max(0.0, next_at - time.monotonic()))
                next_at = time.monotonic() + gap
                try:
                    await mc.commands.send_channel_data(
                        channel, data_type, build_chunk(tid, index, chunks[index]))
                except Exception as exc:
                    bar.write(f"  chunk {index} failed: {exc}")
                bar.update(1, f"#{index}")
            bar.close()

            if reply["done"]:
                print("\ntransfer complete (receiver confirmed mid-round)")
                return

            # Ask for the gaps. ROUND_END or the reply itself can be lost, so
            # ask more than once before concluding nobody is listening.
            for ask in range(1, 4):
                # Clear BEFORE announcing, or a fast NACK gets wiped.
                reported.clear()
                reply.update(any=False, ready=False, last_batch=False)
                await mc.commands.send_channel_data(channel, data_type,
                                                    build_small(ROUND_END, tid))
                print(f"\n  round {rnd} done, waiting for the receiver's "
                      f"report (ask {ask})...")
                await collect(time.monotonic() + wait)
                if reply["any"]:
                    break

            if reply["done"]:
                print("\ntransfer complete")
                return
            if not reply["any"]:
                # On a bad link the receiver's reply can be lost as easily as
                # a spore. Rather than quit, resend the set we last knew was
                # missing — blind, but better than abandoning the transfer.
                blind += 1
                if blind <= 2 and rnd < rounds:
                    print(f"  no reply — resending the same {len(to_send)} "
                          f"spore(s) blind (attempt {blind} of 2)")
                    continue
                print("  no reply — the receiver may not be running, or its "
                      "reply didn't make it back")
                break
            blind = 0
            if reply["ready"]:
                # READY means "I'm holding nothing" — e.g. it threw away a
                # page that failed its hash. That is the opposite of done.
                to_send = list(range(len(chunks)))
                print(f"  receiver is holding nothing: resending all "
                      f"{len(to_send)}")
            else:
                to_send = sorted(i for i in reported if i < len(chunks))
                if not to_send:
                    print("\ntransfer complete")
                    return
                print(f"  receiver is missing {len(to_send)} chunk(s)")
        else:
            print(f"\nstill missing {len(to_send)} chunk(s) after {rounds} rounds")
    finally:
        poller.cancel()


# ---------------------------------------------------------------- receiving

async def recv_page(mc, channel: int, out: Path | None, folder: Path,
                    seconds: int, quiet: float, data_type: int,
                    idle: float = 120.0, flush: bool = True) -> None:
    state: dict = {
        "manifest": None, "chunks": {}, "done": False, "bar": None, "out": out,
        "heard": 0.0,        # last time the SENDER was heard (never our own NACKs)
        "nacked": 0.0,       # last time we transmitted a NACK
        "backoff": quiet,    # grows while the sender stays silent
        "round_end": False,  # the sender says a round just finished
        "drained": 0.0,      # last time the radio said its queue was empty
    }

    def mark_drained() -> None:
        state["drained"] = time.monotonic()

    async def send_report(m: dict) -> None:
        """Tell the sender what we still need, or that we have nothing."""
        missing = [i for i in range(m["total"]) if i not in state["chunks"]]
        if not missing:
            await mc.commands.send_channel_data(channel, data_type,
                                                build_small(DONE, m["tid"]))
            return
        if not state["chunks"]:
            await mc.commands.send_channel_data(channel, data_type,
                                                build_small(READY, m["tid"]))
        else:
            for n, packet in enumerate(nack_batches(m["tid"], missing)):
                if n:
                    await asyncio.sleep(1.0)
                await mc.commands.send_channel_data(channel, data_type, packet)
        state["nacked"] = time.monotonic()

    async def on_data(event) -> None:
        raw = payload_bytes(event, data_type)
        if not raw:
            return
        kind = raw[0]

        if kind == MANIFEST:
            m = parse_manifest(raw)
            state["heard"] = time.monotonic()
            current = state["manifest"]
            same = current and (current["hash"], current["flags"], current["total"],
                                current["length"]) == (m["hash"], m["flags"],
                                                       m["total"], m["length"])
            if same:
                # A resent manifest, or a sender that restarted with a new
                # transfer id. Keep the chunks we already hold in memory.
                state["manifest"] = dict(m)
                await send_report(m)
                return

            target = resolve_out(out, m["name"], folder)
            state["out"] = target
            resumed = load_partial(target, m)
            state.update(manifest=m, chunks=dict(resumed), done=False,
                         backoff=quiet, round_end=False)
            comp = " (compressed)" if m["flags"] & FLAG_ZLIB else ""
            print(f"\n  incoming: {m['name']!r}, {m['total']} chunks, "
                  f"{m['length']} bytes{comp}, hash {m['hash'].hex()}")
            print(f"  will write to: {target}")
            if resumed:
                print(f"  resuming: {len(resumed)} chunk(s) already held")
            if state["bar"]:
                state["bar"].close()
            state["bar"] = Progress(m["total"], "receiving")
            if resumed:
                state["bar"].update(len(resumed))
            await send_report(m)

        elif kind == CHUNK:
            m = state["manifest"]
            if not m:
                return
            _, tid, index = struct.unpack("<BHH", raw[:CHUNK_HEADER])
            if tid != m["tid"] or index >= m["total"]:
                return
            first_time = index not in state["chunks"]
            state["chunks"][index] = raw[CHUNK_HEADER:]
            state["heard"] = time.monotonic()
            if first_time:
                state["backoff"] = quiet          # progress resets the backoff
                if state["bar"]:
                    state["bar"].update(1, f"#{index}")
                if len(state["chunks"]) % 25 == 0:
                    save_partial(state["out"], m, state["chunks"])

        elif kind == ROUND_END:
            m = state["manifest"]
            if m and struct.unpack("<H", raw[1:3])[0] == m["tid"]:
                state["heard"] = time.monotonic()
                state["round_end"] = True

    async def finish(m: dict) -> bool:
        if state["bar"]:
            state["bar"].close()
            state["bar"] = None
        blob = b"".join(state["chunks"][i] for i in range(m["total"]))
        try:
            content = zlib.decompress(blob) if m["flags"] & FLAG_ZLIB else blob
        except zlib.error as exc:
            print(f"\n  could not decompress ({exc}) — discarding and asking "
                  f"for the page again")
            partial_path(state["out"]).unlink(missing_ok=True)
            state["chunks"] = {}
            await send_report(m)          # tell the sender to start over
            return False
        if short_hash(content) != m["hash"]:
            # One spore is wrong and we can't tell which, so everything goes
            # and we ask again — but we must ASK, not fall silent.
            print("\n  HASH MISMATCH — discarding what we have and asking for "
                  "the page again")
            partial_path(state["out"]).unlink(missing_ok=True)
            state["chunks"] = {}
            await send_report(m)
            return False

        final = state["out"] if out is not None else unique_path(state["out"])
        final.write_bytes(content)
        print(f"\n  reassembled {len(content)} bytes")
        print(f"  hash matches: {short_hash(content).hex()}")
        print(f"  wrote {final}")
        partial_path(state["out"]).unlink(missing_ok=True)
        await mc.commands.send_channel_data(channel, data_type,
                                            build_small(DONE, m["tid"]))
        state["done"] = True
        return True

    async def drain_now(limit: int = 300) -> None:
        """Ask the radio for everything it's holding, right now.

        Polling leaves a blind window: datagrams can arrive while the poller
        sleeps, so "I haven't heard a chunk recently" is not evidence they
        weren't sent. Before concluding anything, empty the queue.
        """
        for _ in range(limit):
            try:
                result = await mc.commands.get_msg()
            except Exception:
                return
            if getattr(result, "type", None) in (EventType.NO_MORE_MSGS,
                                                 EventType.ERROR):
                mark_drained()
                return
            await asyncio.sleep(0)      # let the handler above run

    # Anything already queued on the radio is from BEFORE we started: an
    # announcement from a previous run, possibly hours old. Binary datagrams
    # carry no timestamp, so there is no way to tell age after the fact —
    # the only safe moment to discard them is now, before we're listening.
    if flush:
        stale = 0
        while True:
            try:
                result = await mc.commands.get_msg()
            except Exception:
                break
            if getattr(result, "type", None) in (EventType.NO_MORE_MSGS,
                                                 EventType.ERROR):
                break
            stale += 1
            if stale > 500:
                break
        if stale:
            print(f"discarded {stale} message(s) that were already queued on "
                  f"the radio before this run")

    mc.subscribe(EventType.CHANNEL_DATA_RECV, on_data)
    poller = await start_polling(mc, on_idle=mark_drained)
    print(f"waiting up to {seconds}s for a page on channel {channel}, "
          f"then giving up only after {idle:.0f}s of silence...")

    started_waiting = time.monotonic()
    try:
        while not state["done"]:
            await asyncio.sleep(0.25)
            now = time.monotonic()
            m = state["manifest"]

            if m is None:
                if now - started_waiting > seconds:
                    print("\nno page announced — is the sender running?")
                    break
                continue

            # Silence from the SENDER ends the transfer. Our own NACKs don't
            # count, or a dead sender would leave us transmitting forever.
            if now - state["heard"] > idle:
                print(f"\nnothing heard for {idle:.0f}s — the sender seems to "
                      f"have stopped")
                break

            if len(state["chunks"]) >= m["total"]:
                if await finish(m):
                    break
                continue

            if not state["chunks"]:
                # Nothing held (fresh, or just discarded after a bad hash).
                # Answer ROUND_END anyway so the sender knows to resend.
                if state["round_end"] and now - state["nacked"] >= state["backoff"]:
                    state["round_end"] = False
                    print("  holding nothing — asking for the whole page")
                    await send_report(m)
                    state["backoff"] = min(state["backoff"] * 2, 60.0)
                continue

            due = state["round_end"] or (now - state["heard"] >= quiet
                                         and now - state["nacked"] >= state["backoff"])
            if not due:
                continue

            # Empty the radio's queue before deciding anything is missing,
            # then re-check: what looked like a gap is often just lag.
            before = len(state["chunks"])
            await drain_now()
            if len(state["chunks"]) != before:
                state["round_end"] = False
                continue
            if len(state["chunks"]) >= m["total"]:
                if await finish(m):
                    break
                continue

            state["round_end"] = False
            missing = [i for i in range(m["total"]) if i not in state["chunks"]]
            if state["bar"]:
                state["bar"].close()
                state["bar"] = None
            print(f"\n  missing {len(missing)} of {m['total']} chunk(s): "
                  f"{missing[:20]}{' ...' if len(missing) > 20 else ''}")
            batches = nack_batches(m["tid"], missing)
            print(f"  asking for a resend ({len(batches)} request datagram(s))")
            for n, packet in enumerate(batches):
                if n:
                    await asyncio.sleep(1.0)
                await mc.commands.send_channel_data(channel, data_type, packet)
            state["nacked"] = time.monotonic()
            # Back off if the sender stays silent, so we don't hold the
            # channel open forever asking a dead node for chunks.
            state["backoff"] = min(state["backoff"] * 2, 60.0)
            state["bar"] = Progress(m["total"], "receiving")
            state["bar"].update(len(state["chunks"]))
    finally:
        poller.cancel()
        # Save on the way out however we got here — Ctrl-C included.
        if not state["done"] and state["manifest"] and state["chunks"]:
            saved = save_partial(state["out"], state["manifest"], state["chunks"])
            if state["bar"]:
                state["bar"].close()
            print(f"\nkept {len(state['chunks'])}/{state['manifest']['total']} "
                  f"chunks in {saved.name}")
            print("Run the receiver again and resend — both sides pick up here.")


# ---------------------------------------------------------------- main

async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--channel", type=int, required=True)
    ap.add_argument("--send", metavar="FILE", help="page to transmit")
    ap.add_argument("--recv", action="store_true")
    ap.add_argument("--out", help="name for the received file; leave the "
                                  "extension off and the sender's is used")
    ap.add_argument("--dir", default=".", help="folder to write received files into")
    ap.add_argument("--zlib", action=argparse.BooleanOptionalAction, default=True,
                    help="compress before sending (on by default)")
    ap.add_argument("--gap", type=float, default=1.0,
                    help="seconds between chunks; raise it on slow presets")
    ap.add_argument("--rounds", type=int, default=4, help="repair attempts")
    ap.add_argument("--wait", type=float, default=15.0,
                    help="seconds to wait for the receiver's report")
    ap.add_argument("--handshake", type=float, default=4.0,
                    help="seconds to wait for the receiver to say what it needs")
    ap.add_argument("--quiet", type=float, default=6.0,
                    help="fallback silence before the receiver reports gaps")
    ap.add_argument("--seconds", type=int, default=300,
                    help="how long to wait for a page to be announced")
    ap.add_argument("--idle", type=float, default=120.0,
                    help="give up after this much silence mid-transfer")
    ap.add_argument("--data-type", type=lambda s: int(s, 0), default=MOSS_DATA_TYPE)
    ap.add_argument("--keep-queued", action="store_true",
                    help="act on messages the radio queued BEFORE this run "
                         "started, instead of discarding them as stale")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation on long transfers")
    ap.add_argument("--yes-really-public", action="store_true")
    args = ap.parse_args()

    if args.channel == 0 and not args.yes_really_public:
        raise SystemExit("Slot 0 is normally the Public channel. Use your "
                         "testing channel, or pass --yes-really-public.")
    if not args.send and not args.recv:
        raise SystemExit("pass --send FILE or --recv")

    # Ask about a long transfer BEFORE opening the port, so cancelling at the
    # prompt never ties up the radio.
    if args.send:
        path = Path(args.send)
        if not path.exists():
            raise SystemExit(f"no such file: {path}")
        blob, _ = pack(path.read_bytes(), args.zlib)
        count = max(1, math.ceil(len(blob) / CHUNK_BYTES))
        estimate = estimate_seconds(count, args.gap)
        if estimate > 300 and not args.yes:
            answer = input(f"{path.name} is about {fmt_time(estimate)} of airtime "
                           f"on a shared channel. Continue? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                raise SystemExit("cancelled")

    port = args.port or pick_port()
    mc = await open_radio(port, args.baud)
    print(f"connected to {port}")

    try:
        if args.recv:
            await recv_page(mc, args.channel,
                            Path(args.out) if args.out else None,
                            Path(args.dir), args.seconds, args.quiet,
                            args.data_type, args.idle, not args.keep_queued)
        else:
            await send_page(mc, args.channel, Path(args.send), args.gap,
                            args.rounds, args.data_type, args.zlib, args.wait,
                            args.handshake)
    finally:
        await mc.disconnect()
        await asyncio.sleep(0.5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")