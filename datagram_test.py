"""Two-node test using binary datagrams instead of chat messages.

Same radio, same channel, same frequency as the chat test — the difference is
that the payload is opaque binary tagged with an application id, so chat
clients ignore it instead of showing "ping 3" to everyone on the channel.

    pip install meshcore pyserial

Find your testing channel's slot, then:

    python3 datagram_test.py --listen                       # machine A
    python3 datagram_test.py --channel 2 --send 10          # machine B
    python3 datagram_test.py --channel 2 --send 10 --size 163

Datagrams carry at most 163 bytes. This script puts a 4-byte header in front
(sequence number and payload length) and fills the rest with a known pattern,
so the receiver can report exactly which ones went missing and whether the
bytes arrived unchanged.
"""

import argparse
import asyncio
import struct
import time

from meshcore import MeshCore, EventType
from serial.tools import list_ports

# 0xFF00-0xFFFE is the experimental range: free to use while developing, no
# registration needed. 0xFFFF is the shared developer namespace, which other
# people's experiments also use, so a private-ish number is friendlier.
MOSS_DATA_TYPE = 0xFF01
MAX_DATAGRAM = 163
HEADER = 4  # seq (2 bytes) + length (2 bytes)


def make_packet(seq: int, size: int) -> bytes:
    """seq, length, then a repeatable pattern the receiver can verify."""
    size = max(HEADER, min(size, MAX_DATAGRAM))
    body = bytes((seq * 7 + i) % 251 for i in range(size - HEADER))
    return struct.pack("<HH", seq, len(body)) + body


def check_packet(raw: bytes) -> tuple[int, bool]:
    """Returns (sequence number, whether the body is intact)."""
    seq, length = struct.unpack("<HH", raw[:HEADER])
    body = raw[HEADER:]
    expected = bytes((seq * 7 + i) % 251 for i in range(length))
    return seq, len(body) == length and body == expected


def show_ports() -> None:
    ports = list(list_ports.comports())
    if not ports:
        print("no serial ports found — check the cable carries data")
        return
    print("serial ports on this machine:\n")
    for p in ports:
        print(f"  {p.device:20} {p.description}")


def pick_port() -> str:
    ports = [p for p in list_ports.comports()
             if "bluetooth" not in p.description.lower()]
    if not ports:
        raise SystemExit("no serial ports found. Plug in the radio and check "
                         "the USB cable carries data.")
    if len(ports) == 1:
        print(f"using {ports[0].device} ({ports[0].description})")
        return ports[0].device
    print("more than one serial port found:\n")
    for p in ports:
        print(f"  {p.device:20} {p.description}")
    raise SystemExit("\npick one with --port.")


async def list_channels(mc, upto: int = 8) -> None:
    print("channel slots on this radio:\n")
    for idx in range(upto):
        try:
            result = await mc.commands.get_channel(idx)
            payload = getattr(result, "payload", result)
            name = ""
            if isinstance(payload, dict):
                name = payload.get("channel_name") or payload.get("name") or ""
            if name:
                print(f"  {idx}: {name}")
        except Exception as exc:
            print(f"  {idx}: (could not read: {exc})")
            break
    print("\nUse the number next to your testing channel as --channel.")


async def listen(mc, seconds: int, data_type: int, show_all: bool = False) -> None:
    received: dict[int, float] = {}
    corrupt: list[int] = []
    started = time.monotonic()

    async def on_data(event) -> None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        dtype = payload.get("data_type")
        # The library hands back 'payload' as a HEX STRING, not bytes.
        raw = payload.get("payload")
        if isinstance(raw, str):
            try:
                raw = bytes.fromhex(raw)
            except ValueError:
                print(f"  (undecodable payload: {raw!r})")
                return
        if dtype is not None and dtype != data_type:
            print(f"  (ignored data type 0x{dtype:04X}, ch {payload.get('channel_idx')})")
            return
        if not isinstance(raw, (bytes, bytearray)) or len(raw) < HEADER:
            print(f"  (short or unreadable frame: {payload!r})")
            return
        seq, intact = check_packet(bytes(raw))
        received[seq] = time.monotonic()
        flag = "" if intact else "  CORRUPT"
        if not intact:
            corrupt.append(seq)
        snr = payload.get("SNR")
        snr_text = f"  SNR {snr}" if snr is not None else ""
        hops = payload.get("path_len")
        hop_text = "" if hops in (None, 255) else f"  {hops} hop(s)"
        print(f"  seq {seq:3d}  {len(raw):3d} bytes  "
              f"t+{received[seq] - started:6.1f}s{snr_text}{hop_text}{flag}")

    async def on_any(event) -> None:
        if show_all:
            print(f"  RAW {event.payload!r}")

    if show_all:
        mc.subscribe(EventType.CHANNEL_DATA_RECV, on_any)
    mc.subscribe(EventType.CHANNEL_DATA_RECV, on_data)

    # The radio does NOT push received datagrams up the serial link. It pushes
    # MESSAGES_WAITING, and the app must then call get_msg() for each one.
    # Without this, messages queue on the radio and nothing ever arrives here.
    await mc.start_auto_message_fetching()

    print(f"listening {seconds}s for data type 0x{data_type:04X}\n")
    await asyncio.sleep(seconds)

    if not received:
        print("\nnothing arrived. Check both radios are on the same channel and "
              "frequency preset, both are using the same --data-type, and the "
              "firmware is new enough to carry channel datagrams.")
        return

    seqs = sorted(received)
    expected = seqs[-1] + 1
    missing = [s for s in range(expected) if s not in received]
    span = received[seqs[-1]] - received[seqs[0]] or 1e-9
    print(f"\n{len(received)}/{expected} arrived ({len(missing) / expected:.0%} lost)")
    if missing:
        print(f"missing: {missing}")
    if corrupt:
        print(f"corrupt: {corrupt}")
    print(f"{len(received) / span:.2f} datagrams/s while the sender was running")


async def send(mc, channel: int, count: int, size: int, gap: float,
               data_type: int) -> None:
    print(f"sending {count} datagrams of {size} bytes on channel {channel}, "
          f"data type 0x{data_type:04X}, {gap}s apart\n")
    started = time.monotonic()
    failures = 0
    for seq in range(count):
        t0 = time.monotonic()
        try:
            result = await mc.commands.send_channel_data(channel, data_type,
                                                         make_packet(seq, size))
        except Exception as exc:
            print(f"  seq {seq:3d}  raised: {exc}")
            failures += 1
            if failures >= 3:
                print("\nthree failures in a row — stopping. Run diagnose.py.")
                return
            await asyncio.sleep(gap)
            continue

        status = getattr(result, "type", result)
        payload = getattr(result, "payload", None)
        note = ""
        if status == EventType.ERROR:
            failures += 1
            # {"reason": "timeout"} means the radio never answered at all,
            # which is different from the firmware rejecting the command.
            note = f"  <- {payload!r}"
        else:
            failures = 0
        print(f"  seq {seq:3d}  {time.monotonic() - t0:5.2f}s  {status}{note}")

        if failures >= 3:
            print("\nthree errors in a row — stopping rather than hammering the "
                  "radio. Run diagnose.py to see what the firmware says.")
            return
        await asyncio.sleep(gap)
    elapsed = time.monotonic() - started
    print(f"\n{count} datagrams in {elapsed:.1f}s")
    print("The radio only reports that it accepted them — run --listen on the "
          "other node to find out what actually arrived.")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="COM3 on Windows, /dev/cu.* on macOS, "
                                   "/dev/tty* on Linux. Omit to auto-detect.")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--ports", action="store_true", help="list serial ports and exit")
    ap.add_argument("--channels", action="store_true", help="list channel slots and exit")
    ap.add_argument("--listen", action="store_true")
    ap.add_argument("--send", type=int, metavar="N")
    ap.add_argument("--channel", type=int, help="channel slot to send on")
    ap.add_argument("--size", type=int, default=64,
                    help=f"datagram size in bytes, {HEADER}-{MAX_DATAGRAM}")
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--gap", type=float, default=3.0,
                    help="seconds between sends; raise it on slow presets "
                         "(high SF, narrow bandwidth) where airtime is longer")
    ap.add_argument("--data-type", type=lambda s: int(s, 0), default=MOSS_DATA_TYPE,
                    help="application id, e.g. 0xFF01 (same on both ends)")
    ap.add_argument("--all", action="store_true",
                    help="while listening, dump every datagram event as it "
                         "arrives, before any filtering")
    ap.add_argument("--yes-really-public", action="store_true")
    args = ap.parse_args()

    if args.ports:
        show_ports()
        return
    if args.send and args.channel is None:
        raise SystemExit("--channel is required for sending. "
                         "Run --channels to see the slots on this radio.")
    if args.send and args.channel == 0 and not args.yes_really_public:
        raise SystemExit("Slot 0 is normally the Public channel. Pick your testing "
                         "channel's slot, or pass --yes-really-public if you mean it.")

    port = args.port or pick_port()
    mc = await MeshCore.create_serial(port, args.baud)
    print(f"connected to {port}")

    try:
        if args.channels:
            await list_channels(mc)
        elif args.listen:
            await listen(mc, args.seconds, args.data_type, args.all)
        elif args.send:
            await send(mc, args.channel, args.send, args.size, args.gap,
                       args.data_type)
        else:
            print("pass --ports, --channels, --listen, or --send N")
    finally:
        # Release the serial port, or the next run gets "access is denied".
        await mc.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")