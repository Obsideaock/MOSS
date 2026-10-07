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
import random
import struct
import time
import zlib
from pathlib import Path

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


# ---------------------------------------------------------------- sending

async def send_page(mc, channel: int, path: Path, gap: float, rounds: int,
                    data_type: int, use_zlib: bool, wait: float) -> None:
    content = path.read_bytes()
    digest = short_hash(content)
    flags = 0
    blob = content
    if use_zlib:
        blob = zlib.compress(content, 9)
        flags |= FLAG_ZLIB

    chunks = [blob[i:i + CHUNK_BYTES] for i in range(0, len(blob), CHUNK_BYTES)]
    tid = random.randint(1, 0xFFFF)

    print(f"\n{path.name}: {len(content)} bytes"
          + (f" -> {len(blob)} compressed ({len(blob)/len(content):.0%})" if use_zlib else "")
          + f"\n{len(chunks)} chunks of up to {CHUNK_BYTES} bytes, "
            f"transfer id {tid}, hash {digest.hex()}\n")

    # The sender also needs to hear NACKs coming back.
    nacks: list[list[int]] = []

    async def on_data(event) -> None:
        raw = payload_bytes(event, data_type)
        if not raw or raw[0] not in (NACK, DONE):
            return
        if raw[0] == DONE and struct.unpack("<H", raw[1:3])[0] == tid:
            print("  receiver confirms the page arrived and the hash matched")
            nacks.append([])
        elif raw[0] == NACK:
            ntid, missing = parse_nack(raw)
            if ntid == tid:
                nacks.append(missing)

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
            if rnd > 1:
                print(f"\n  repair round {rnd - 1}: resending {len(to_send)} chunk(s)")
            for index in to_send:
                await mc.commands.send_channel_data(
                    channel, data_type, build_chunk(tid, index, chunks[index]))
                print(f"  chunk {index:3d}/{len(chunks) - 1}  "
                      f"{len(chunks[index]):3d} bytes")
                await asyncio.sleep(gap)

            print(f"\n  waiting {wait:.0f}s for the receiver to report gaps...")
            nacks.clear()
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and not nacks:
                await asyncio.sleep(0.25)

            if not nacks:
                print("  no reply — the receiver may not be running, or its "
                      "reply didn't make it back")
                break
            to_send = nacks[-1]
            if not to_send:
                print("\ntransfer complete")
                return
        else:
            print(f"\nstill missing {len(to_send)} chunk(s) after {rounds} rounds")
    finally:
        poller.cancel()


# ---------------------------------------------------------------- receiving

async def recv_page(mc, channel: int, out: Path, seconds: int, quiet: float,
                    data_type: int) -> None:
    state: dict = {"manifest": None, "chunks": {}, "last": 0.0, "done": False}

    async def on_data(event) -> None:
        raw = payload_bytes(event, data_type)
        if not raw:
            return
        kind = raw[0]

        if kind == MANIFEST:
            m = parse_manifest(raw)
            state["manifest"] = m
            state["chunks"] = {}
            state["done"] = False
            state["last"] = time.monotonic()
            comp = " (compressed)" if m["flags"] & FLAG_ZLIB else ""
            print(f"\n  incoming: {m['name']!r}, {m['total']} chunks, "
                  f"{m['length']} bytes{comp}, hash {m['hash'].hex()}")

        elif kind == CHUNK:
            m = state["manifest"]
            if not m:
                return  # chunks for a page we never saw announced
            _, tid, index = struct.unpack("<BHH", raw[:CHUNK_HEADER])
            if tid != m["tid"]:
                return
            state["chunks"][index] = raw[CHUNK_HEADER:]
            state["last"] = time.monotonic()
            got, total = len(state["chunks"]), m["total"]
            print(f"  chunk {index:3d}  {len(raw) - CHUNK_HEADER:3d} bytes   "
                  f"{got}/{total}")

    mc.subscribe(EventType.CHANNEL_DATA_RECV, on_data)
    poller = await start_polling(mc)
    print(f"waiting up to {seconds}s for a page on channel {channel}...")

    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline and not state["done"]:
            await asyncio.sleep(0.25)
            m = state["manifest"]
            if not m or not state["chunks"]:
                continue
            # Once the air has been quiet for a moment, report what's missing.
            if time.monotonic() - state["last"] < quiet:
                continue

            missing = [i for i in range(m["total"]) if i not in state["chunks"]]
            if missing:
                print(f"\n  missing {len(missing)} chunk(s): {missing[:20]}"
                      f"{' ...' if len(missing) > 20 else ''}")
                print("  asking for a resend")
                await mc.commands.send_channel_data(
                    channel, data_type, build_nack(m["tid"], missing))
                state["last"] = time.monotonic()
                continue

            blob = b"".join(state["chunks"][i] for i in range(m["total"]))
            content = zlib.decompress(blob) if m["flags"] & FLAG_ZLIB else blob
            ok = short_hash(content) == m["hash"]

            print(f"\n  reassembled {len(content)} bytes")
            if not ok:
                print("  HASH MISMATCH — not writing the file")
                state["done"] = True
                break

            out.write_bytes(content)
            print(f"  hash matches: {short_hash(content).hex()}")
            print(f"  wrote {out}")
            await mc.commands.send_channel_data(
                channel, data_type, struct.pack("<BH", DONE, m["tid"]))
            state["done"] = True
    finally:
        poller.cancel()

    if not state["done"]:
        print("\ntimed out")


# ---------------------------------------------------------------- main

async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--channel", type=int, required=True)
    ap.add_argument("--send", metavar="FILE", help="page to transmit")
    ap.add_argument("--recv", action="store_true")
    ap.add_argument("--out", default="received_page.md")
    ap.add_argument("--zlib", action="store_true", help="compress before sending")
    ap.add_argument("--gap", type=float, default=1.0,
                    help="seconds between chunks; a full datagram is about "
                         "half a second of airtime on a fast preset")
    ap.add_argument("--rounds", type=int, default=4, help="repair attempts")
    ap.add_argument("--wait", type=float, default=15.0,
                    help="seconds to wait for the receiver's reply")
    ap.add_argument("--quiet", type=float, default=6.0,
                    help="seconds of silence before the receiver reports gaps")
    ap.add_argument("--seconds", type=int, default=300, help="receiver timeout")
    ap.add_argument("--data-type", type=lambda s: int(s, 0), default=MOSS_DATA_TYPE)
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
            await recv_page(mc, args.channel, Path(args.out), args.seconds,
                            args.quiet, args.data_type)
        else:
            path = Path(args.send)
            if not path.exists():
                raise SystemExit(f"no such file: {path}")
            await send_page(mc, args.channel, path, args.gap, args.rounds,
                            args.data_type, args.zlib, args.wait)
    finally:
        await mc.disconnect()
        await asyncio.sleep(0.5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
