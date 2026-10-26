"""Simplest possible two-node test: send numbered messages, see how many arrive.

    pip install meshcore pyserial

Serial port names differ by platform:
    Windows   COM3, COM4, ...
    macOS     /dev/cu.usbserial-0001, /dev/cu.usbmodem14201, ...
    Linux/Pi  /dev/ttyUSB0, /dev/ttyACM0, ...

So start by seeing what's plugged in — you can leave --port off entirely and
the script picks the port if exactly one radio-ish device is attached:
    python3 ping_test.py --ports

FIRST, find your testing channel's slot number on each radio:
    python3 ping_test.py --channels

Then, on the receiving machine:
    python3 ping_test.py --listen

And on the sending machine (use YOUR testing channel's number):
    python3 ping_test.py --channel 2 --send 10

--channel is required for sending. Slot 0 is normally the Public channel and
is refused unless you pass --yes-really-public. Channel numbering is per
radio, so check it on each one; the same channel can sit in different slots.
"""

import argparse
import asyncio
import time

from meshcore import MeshCore, EventType
from serial.tools import list_ports


def show_ports():
    ports = list(list_ports.comports())
    if not ports:
        print("no serial ports found — is the radio plugged in and the cable a "
              "data cable, not charge-only?")
        return
    print("serial ports on this machine:\n")
    for p in ports:
        print(f"  {p.device:20} {p.description}")
    print("\nPass the left-hand name as --port.")


def pick_port():
    """Guess the radio's port when only one plausible device is attached."""
    ports = list(list_ports.comports())
    if not ports:
        raise SystemExit("no serial ports found. Plug in the radio, and check "
                         "that the USB cable carries data.")
    # Filter out things that are obviously not a radio (Bluetooth bridges, etc.)
    candidates = [p for p in ports if "bluetooth" not in p.description.lower()]
    if len(candidates) == 1:
        print(f"using {candidates[0].device} ({candidates[0].description})")
        return candidates[0].device
    print("more than one serial port found:\n")
    for p in candidates:
        print(f"  {p.device:20} {p.description}")
    raise SystemExit("\npick one with --port.")


async def open_radio(port: str, baud: int, attempts: int = 6):
    """Open the port, retrying while Windows finishes releasing it.

    After a previous run exits, the USB-serial driver can hold the handle for
    a second or two, so an immediate re-run gets "access is denied".
    """
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
            busy = ("access is denied" in text or "permission" in text
                    or "could not open port" in text or "in use" in text)
            if not busy:
                raise
            print(f"  {port} busy (attempt {n}/{attempts}), waiting...")
            await asyncio.sleep(1.5)
    raise SystemExit(
        f"\n{port} stayed busy. Something else is holding it:\n"
        "  - another terminal still running one of these scripts\n"
        "  - a stray python.exe (check Task Manager)\n"
        "  - the MeshCore desktop or web client connected to the radio\n"
        f"Last error: {last}"
    )


async def list_channels(mc, upto=8):
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


async def listen(mc, seconds):
    seen = []

    async def on_msg(event):
        payload = event.payload
        text = payload.get("text", "")
        where = payload.get("channel_idx", payload.get("pubkey_prefix", "?"))
        print(f"  [{time.strftime('%H:%M:%S')}] (ch {where}) {text}")
        seen.append(text)

    mc.subscribe(EventType.CHANNEL_MSG_RECV, on_msg)
    mc.subscribe(EventType.CONTACT_MSG_RECV, on_msg)

    # The radio queues incoming messages and only pushes MESSAGES_WAITING;
    # this makes the library call get_msg() to actually pull them down.
    await mc.start_auto_message_fetching()

    print(f"listening for {seconds}s...\n")
    await asyncio.sleep(seconds)
    print(f"\ngot {len(seen)} messages")


async def send(mc, channel, count, gap):
    print(f"sending {count} messages on channel {channel}, {gap}s apart\n")
    for i in range(count):
        text = f"ping {i}"
        start = time.time()
        await mc.commands.send_chan_msg(channel, text)
        print(f"  sent {text!r} in {time.time() - start:.2f}s")
        await asyncio.sleep(gap)
    print(f"\nsent {count} messages")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="COM3 on Windows, /dev/cu.* on macOS, "
                                   "/dev/tty* on Linux. Omit to auto-detect.")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--ports", action="store_true", help="list serial ports and exit")
    ap.add_argument("--channels", action="store_true", help="list channel slots and exit")
    ap.add_argument("--listen", action="store_true")
    ap.add_argument("--send", type=int, metavar="N")
    ap.add_argument("--channel", type=int, help="channel slot to send on")
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--gap", type=float, default=3.0)
    ap.add_argument("--yes-really-public", action="store_true",
                    help="allow sending on slot 0 (the public channel)")
    args = ap.parse_args()

    if args.ports:
        show_ports()
        return

    if args.send and args.channel is None:
        raise SystemExit("--channel is required for sending. "
                         "Run with --channels to see the slots on this radio.")
    if args.send and args.channel == 0 and not args.yes_really_public:
        raise SystemExit("Slot 0 is normally the Public channel. Pick your testing "
                         "channel's slot, or pass --yes-really-public if you mean it.")

    port = args.port or pick_port()
    mc = await open_radio(port, args.baud)
    print(f"connected to {port}")

    try:
        if args.channels:
            await list_channels(mc)
        elif args.listen:
            await listen(mc, args.seconds)
        elif args.send:
            await send(mc, args.channel, args.send, args.gap)
        else:
            print("pass --channels, --listen, or --send N")
    finally:
        # Release the serial port, or the next run gets "access is denied".
        await mc.disconnect()
        await asyncio.sleep(0.5)  # let the driver release the port


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")