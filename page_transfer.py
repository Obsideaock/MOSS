"""Send a whole page across the mesh, chunk by chunk, and verify it arrived.

This is MOSS phase 3 in its simplest honest form: one publisher, one
subscriber, one file, no history, no signatures yet. The content is hashed
before it leaves and checked after it lands, so a corrupted or incomplete
page is rejected rather than written.

    # receiver first — it waits for a manifest
    python page_transfer.py --port COM12 --channel 1 --recv --out got.md

    # then the sender
    python page_transfer.py --port COM16 --channel 1 --send sample_page.md

Optionally compress before sending (usually a big win on text):
    python page_transfer.py --port COM16 --channel 1 --send sample_page.md --zlib

How it works on the wire, all inside 163-byte datagrams:

    MANIFEST  how many chunks are coming, how long, the content hash, the name
    CHUNK     one numbered slice of the (optionally compressed) bytes
    NACK      receiver's list of the chunk numbers it never got
    DONE      receiver confirming the hash matched

The sender transmits every chunk, then listens for a NACK and resends only
what's missing, up to --rounds times. That's the smallest thing that can
still survive packet loss.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import struct
import time
import zlib
from pathlib import Path, PurePosixPath

from meshcore import MeshCore, EventType
from serial.tools import list_ports

MOSS_DATA_TYPE = 0xFF01
MAX_DATAGRAM = 163

MANIFEST, CHUNK, NACK, DONE = 1, 2, 3, 4
CHUNK_HEADER = 5          # type(1) + tid(2) + index(2)
CHUNK_BYTES = MAX_DATAGRAM - CHUNK_HEADER   # 158 bytes of page per datagram

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
    digest = raw[10:18]
    name_len = raw[18]
    return {"tid": tid, "total": total, "length": length, "flags": flags,
            "hash": digest, "name": raw[19:19 + name_len].decode(errors="replace")}


def build_chunk(tid: int, index: int, payload: bytes) -> bytes:
    return struct.pack("<BHH", CHUNK, tid, index) + payload


def build_nack(tid: int, missing: list[int]) -> bytes:
    """As many missing indices as fit in one datagram."""
    room = (MAX_DATAGRAM - 4) // 2
    chosen = missing[:room]
    return (struct.pack("<BHB", NACK, tid, len(chosen))
            + b"".join(struct.pack("<H", i) for i in chosen))


def parse_nack(raw: bytes) -> tuple[int, list[int]]:
    _, tid, count = struct.unpack("<BHB", raw[:4])
    return tid, [struct.unpack("<H", raw[4 + 2 * i:6 + 2 * i])[0]
                 for i in range(count)]


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


async def start_polling(mc, interval: float = 1.0) -> asyncio.Task:
    """The radio queues messages; nothing arrives unless we ask for them."""
    async def loop() -> None:
        while True:
            try:
                result = await mc.commands.get_msg()
                if getattr(result, "type", None) in (EventType.NO_MORE_MSGS,
                                                     EventType.ERROR):
                    await asyncio.sleep(interval)
                else:
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"  (poll error: {exc})")
                await asyncio.sleep(interval)
    return asyncio.create_task(loop())



# ---------------------------------------------------------------- progress

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


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
        """Print without stomping on the bar."""
        if self.bar:
            self.bar.write(text)
        else:
            print(f"\n{text}")

    def close(self) -> None:
        if self.bar:
            self.bar.close()
        elif not self.quiet:
            print()


def fmt_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


# ---------------------------------------------------------------- sending

async def send_page(mc, channel: int, path: Path, gap: float, rounds: int,
                    data_type: int, use_zlib: bool, wait: float,
                    assume_yes: bool = False) -> None:
    content = path.read_bytes()
    digest = short_hash(content)
    flags = 0
    blob = content
    if use_zlib:
        blob = zlib.compress(content, 9)
        flags |= FLAG_ZLIB

    chunks = [blob[i:i + CHUNK_BYTES] for i in range(0, len(blob), CHUNK_BYTES)]
    tid = random.randint(1, 0xFFFF)

    estimate = len(chunks) * (gap + 0.55)   # airtime plus the gap between sends
    print(f"\n{path.name}: {len(content)} bytes"
          + (f" -> {len(blob)} compressed ({len(blob)/len(content):.0%})" if use_zlib else "")
          + f"\n{len(chunks)} chunks of up to {CHUNK_BYTES} bytes, "
            f"transfer id {tid}, hash {digest.hex()}\n"
            f"estimated time: {fmt_time(estimate)}\n")

    if estimate > 300 and not assume_yes:
        answer = input(f"That's {fmt_time(estimate)} of airtime on a shared "
                       f"channel. Continue? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("cancelled")
            return

    # The sender also needs to hear NACKs coming back. A big transfer's
    # missing list spans several NACK datagrams, so collect every one that
    # arrives in the window and take the union — using only the last would
    # resend a fraction of what's actually missing.
    reported: set[int] = set()
    got_reply = {"any": False, "done": False}

    async def on_data(event) -> None:
        raw = payload_bytes(event, data_type)
        if not raw or raw[0] not in (NACK, DONE):
            return
        if raw[0] == DONE and struct.unpack("<H", raw[1:3])[0] == tid:
            got_reply["any"] = True
            got_reply["done"] = True
        elif raw[0] == NACK:
            ntid, missing = parse_nack(raw)
            if ntid == tid:
                got_reply["any"] = True
                reported.update(missing)

    mc.subscribe(EventType.CHANNEL_DATA_RECV, on_data)
    poller = await start_polling(mc)

    try:
        await mc.commands.send_channel_data(
            channel, data_type,
            build_manifest(tid, len(chunks), len(blob), digest, path.name, flags))
        print("  manifest sent")
        await asyncio.sleep(gap)

        to_send = list(range(len(chunks)))
        for rnd in range(1, rounds + 1):
            label = "sending" if rnd == 1 else f"repair {rnd - 1}"
            bar = Progress(len(to_send), label)
            for index in to_send:
                try:
                    await mc.commands.send_channel_data(
                        channel, data_type, build_chunk(tid, index, chunks[index]))
                except Exception as exc:
                    bar.write(f"  chunk {index} failed: {exc}")
                bar.update(1, f"#{index}")
                await asyncio.sleep(gap)
            bar.close()

            print(f"\n  waiting up to {wait:.0f}s for the receiver to report gaps...")
            reported.clear()
            got_reply["any"] = got_reply["done"] = False
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and not got_reply["any"]:
                await asyncio.sleep(0.25)

            if got_reply["any"] and not got_reply["done"]:
                # More NACK datagrams may still be on their way.
                await asyncio.sleep(min(wait, 8.0))

            if got_reply["done"]:
                print("  receiver confirms the page arrived and the hash matched")
                print("\ntransfer complete")
                return
            if not got_reply["any"]:
                print("  no reply — the receiver may not be running, or its "
                      "reply didn't make it back")
                break

            to_send = sorted(reported)
            if not to_send:
                print("\ntransfer complete")
                return
            print(f"  receiver is missing {len(to_send)} chunk(s)")
        else:
            print(f"\nstill missing {len(to_send)} chunk(s) after {rounds} rounds")
    finally:
        poller.cancel()



# ---------------------------------------------------------------- resuming


def safe_name(name: str, folder: Path) -> Path:
    """Use the sender's filename, but never let it escape the folder.

    The name arrives over the air from someone else, so it is untrusted:
    strip any directory part, drop anything odd, and fall back to a plain
    default if nothing usable is left.
    """
    base = PurePosixPath(name.replace("\\", "/")).name
    base = "".join(c for c in base if c.isalnum() or c in "._- ").strip()
    while base.startswith("."):
        base = base[1:]
    if not base:
        base = "received_page"
    return folder / base


def unique_path(path: Path) -> Path:
    """Don't silently overwrite a file that's already there."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for n in range(1, 1000):
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    return path



def resolve_out(out: Path | None, sender_name: str, folder: Path) -> Path:
    """Work out where to write, borrowing the extension when one is missing.

    --out receivedphoto   + sender sent TestPhoto.jpg  -> receivedphoto.jpg
    --out receivedphoto.x + sender sent TestPhoto.jpg  -> receivedphoto.x (yours wins)
    no --out              + sender sent TestPhoto.jpg  -> TestPhoto.jpg
    """
    sender_suffix = PurePosixPath(sender_name.replace("\\", "/")).name
    sender_suffix = Path(sender_suffix).suffix
    if out is None:
        return safe_name(sender_name, folder)
    if out.suffix:
        return out            # an extension was given on purpose
    return out.with_suffix(sender_suffix) if sender_suffix else out


def partial_path(out: Path) -> Path:
    return out.with_suffix(out.suffix + ".moss-partial")


def save_partial(out: Path, manifest: dict, chunks: dict[int, bytes]) -> Path:
    """Keep what we have so an interrupted transfer can pick up later."""
    record = {
        "name": manifest["name"],
        "hash": manifest["hash"].hex(),
        "flags": manifest["flags"],
        "total": manifest["total"],
        "length": manifest["length"],
        "chunks": {str(i): c.hex() for i, c in chunks.items()},
    }
    path = partial_path(out)
    path.write_text(json.dumps(record))
    return path


def load_partial(out: Path, manifest: dict) -> dict[int, bytes]:
    """Reload chunks saved earlier, but only for this exact content."""
    path = partial_path(out)
    if not path.exists():
        return {}
    try:
        record = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    # The hash identifies the content; a transfer id does not survive a restart.
    if record.get("hash") != manifest["hash"].hex():
        return {}
    if record.get("total") != manifest["total"]:
        return {}
    return {int(i): bytes.fromhex(h) for i, h in record.get("chunks", {}).items()}


# ---------------------------------------------------------------- receiving

async def recv_page(mc, channel: int, out: Path | None, folder: Path,
                    seconds: int, quiet: float, data_type: int,
                    idle: float = 120.0) -> None:
    state: dict = {"manifest": None, "chunks": {}, "last": 0.0,
                   "done": False, "bar": None, "out": out}

    async def on_data(event) -> None:
        raw = payload_bytes(event, data_type)
        if not raw:
            return
        kind = raw[0]

        if kind == MANIFEST:
            m = parse_manifest(raw)
            # Keep the sender's extension unless --out already spells one
            # out, so a .png never lands as a .jpg.
            target = resolve_out(out, m["name"], folder)
            state["out"] = target
            resumed = load_partial(target, m)
            state["manifest"] = m
            state["chunks"] = dict(resumed)
            state["done"] = False
            state["last"] = time.monotonic()
            comp = " (compressed)" if m["flags"] & FLAG_ZLIB else ""
            print(f"\n  incoming: {m['name']!r}, {m['total']} chunks, "
                  f"{m['length']} bytes{comp}, hash {m['hash'].hex()}")
            if out is not None and out.suffix and target.suffix != Path(m["name"]).suffix:
                print(f"  note: sender called it {m['name']!r}, writing as "
                      f"{target.name} because --out named that extension")
            print(f"  will write to: {target}")
            if resumed:
                print(f"  resuming: {len(resumed)} chunk(s) already held from "
                      f"an earlier attempt")
            if state["bar"]:
                state["bar"].close()
            state["bar"] = Progress(m["total"], "receiving")
            if resumed:
                state["bar"].update(len(resumed))

        elif kind == CHUNK:
            m = state["manifest"]
            if not m:
                return  # chunks for a page we never saw announced
            _, tid, index = struct.unpack("<BHH", raw[:CHUNK_HEADER])
            if tid != m["tid"]:
                return
            first_time = index not in state["chunks"]
            state["chunks"][index] = raw[CHUNK_HEADER:]
            state["last"] = time.monotonic()
            if state["bar"] and first_time:
                state["bar"].update(1, f"#{index}")
            # Checkpoint now and then, so a crash or a closed window doesn't
            # throw away an hour of airtime.
            if first_time and len(state["chunks"]) % 25 == 0:
                save_partial(state["out"], m, state["chunks"])

    mc.subscribe(EventType.CHANNEL_DATA_RECV, on_data)
    poller = await start_polling(mc)
    print(f"waiting up to {seconds}s for a page on channel {channel}, "
          f"then giving up only after {idle:.0f}s of silence...")

    started_waiting = time.monotonic()
    try:
        while not state["done"]:
            # The timeout is for SILENCE, not for the transfer's length. A big
            # page can take an hour; that is not a failure.
            if state["manifest"] is None:
                if time.monotonic() - started_waiting > seconds:
                    print("\nno page announced — is the sender running?")
                    break
            elif time.monotonic() - state["last"] > idle:
                print(f"\nnothing heard for {idle:.0f}s — the sender seems to "
                      f"have stopped")
                break
            await asyncio.sleep(0.25)
            m = state["manifest"]
            if not m or not state["chunks"]:
                continue
            # Once the air has been quiet for a moment, report what's missing.
            if time.monotonic() - state["last"] < quiet:
                continue

            missing = [i for i in range(m["total"]) if i not in state["chunks"]]
            if missing:
                if state["bar"]:
                    state["bar"].close()
                    state["bar"] = None
                print(f"\n  missing {len(missing)} of {m['total']} chunk(s): "
                      f"{missing[:20]}{' ...' if len(missing) > 20 else ''}")
                # One NACK datagram holds only ~79 indices, so a long list
                # goes out as several, and the sender takes their union.
                room = (MAX_DATAGRAM - 4) // 2
                batches = [missing[i:i + room] for i in range(0, len(missing), room)]
                print(f"  asking for a resend ({len(batches)} request datagram(s))")
                for batch in batches:
                    await mc.commands.send_channel_data(
                        channel, data_type, build_nack(m["tid"], batch))
                    await asyncio.sleep(1.0)
                state["bar"] = Progress(m["total"], "receiving")
                state["bar"].update(len(state["chunks"]))
                state["last"] = time.monotonic()
                continue

            if state["bar"]:
                state["bar"].close()
                state["bar"] = None
            blob = b"".join(state["chunks"][i] for i in range(m["total"]))
            content = zlib.decompress(blob) if m["flags"] & FLAG_ZLIB else blob
            ok = short_hash(content) == m["hash"]

            print(f"\n  reassembled {len(content)} bytes")
            if not ok:
                print("  HASH MISMATCH — not writing the file")
                state["done"] = True
                break

            target = state["out"]
            partial = partial_path(target)
            # Only now, with a verified file, avoid clobbering an existing one.
            final = target if out is not None else unique_path(target)
            final.write_bytes(content)
            print(f"  hash matches: {short_hash(content).hex()}")
            print(f"  wrote {final}")
            partial.unlink(missing_ok=True)
            await mc.commands.send_channel_data(
                channel, data_type, struct.pack("<BH", DONE, m["tid"]))
            state["done"] = True
    finally:
        poller.cancel()

    if not state["done"]:
        m, chunks = state["manifest"], state["chunks"]
        if state["bar"]:
            state["bar"].close()
        if m and chunks:
            saved = save_partial(state["out"], m, chunks)
            print(f"\nkept {len(chunks)}/{m['total']} chunks in {saved.name}")
            print("Run the receiver again and resend — it picks up where it "
                  "left off instead of starting over.")
        else:
            print("\nnothing received")


# ---------------------------------------------------------------- main

async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--channel", type=int, required=True)
    ap.add_argument("--send", metavar="FILE", help="page to transmit")
    ap.add_argument("--recv", action="store_true")
    ap.add_argument("--out", help="name for the received file; leave the "
                                  "extension off and the sender's is used. "
                                  "Omit entirely to keep the sender's name")
    ap.add_argument("--dir", default=".",
                    help="folder to write received files into")
    ap.add_argument("--zlib", action="store_true", help="compress before sending")
    ap.add_argument("--gap", type=float, default=1.0,
                    help="seconds between chunks; a full datagram is about "
                         "half a second of airtime on a fast preset")
    ap.add_argument("--rounds", type=int, default=4, help="repair attempts")
    ap.add_argument("--wait", type=float, default=15.0,
                    help="seconds to wait for the receiver's reply")
    ap.add_argument("--quiet", type=float, default=6.0,
                    help="seconds of silence before the receiver reports gaps")
    ap.add_argument("--seconds", type=int, default=300,
                    help="how long to wait for a page to be announced")
    ap.add_argument("--idle", type=float, default=120.0,
                    help="give up after this many seconds of silence "
                         "mid-transfer (not a limit on total time)")
    ap.add_argument("--data-type", type=lambda s: int(s, 0), default=MOSS_DATA_TYPE)
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation on long transfers")
    ap.add_argument("--yes-really-public", action="store_true")
    args = ap.parse_args()

    if args.channel == 0 and not args.yes_really_public:
        raise SystemExit("Slot 0 is normally the Public channel. Use your "
                         "testing channel, or pass --yes-really-public.")
    if not args.send and not args.recv:
        raise SystemExit("pass --send FILE or --recv")

    port = args.port or pick_port()
    mc = await open_radio(port, args.baud)
    print(f"connected to {port}")

    try:
        if args.recv:
            await recv_page(mc, args.channel,
                            Path(args.out) if args.out else None,
                            Path(args.dir), args.seconds,
                            args.quiet, args.data_type, args.idle)
        else:
            path = Path(args.send)
            if not path.exists():
                raise SystemExit(f"no such file: {path}")
            await send_page(mc, args.channel, path, args.gap, args.rounds,
                            args.data_type, args.zlib, args.wait, args.yes)
    finally:
        await mc.disconnect()
        await asyncio.sleep(0.5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")