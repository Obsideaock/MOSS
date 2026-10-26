"""Unstick a radio: drain its message queue, reset the board, tidy up.

Run this when sends start timing out, or the port won't open even after the
retries. The usual cause is the radio's message queue filling up with
messages nothing ever fetched — a full queue makes the firmware slow or
unresponsive to new commands.

    python reset_radio.py                      # drain the queue (safe, default)
    python reset_radio.py --port COM12
    python reset_radio.py --hard               # also pulse the board's reset line
    python reset_radio.py --who                # what's holding the port
    python reset_radio.py --clean-partials     # delete leftover .moss-partial files

Nothing here touches your channels, keys, or settings. "Purge" means the
message backlog and this computer's leftovers, not the radio's config.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import serial
from serial.tools import list_ports

try:
    import psutil
except ImportError:
    psutil = None


def show_ports() -> None:
    ports = list(list_ports.comports())
    if not ports:
        print("no serial ports found")
        return
    print("serial ports:")
    for p in ports:
        print(f"  {p.device:12} {p.description}")


def pick_port() -> str:
    ports = [p for p in list_ports.comports()
             if "bluetooth" not in p.description.lower()]
    if not ports:
        raise SystemExit("no serial ports found")
    if len(ports) == 1:
        print(f"using {ports[0].device} ({ports[0].description})")
        return ports[0].device
    show_ports()
    raise SystemExit("more than one — pick with --port")


def who_has_it(port: str) -> None:
    """Best effort: name the processes that could be holding the port."""
    print(f"\nchecking what might be holding {port} ...")
    if psutil is None:
        print("  (pip install psutil for a proper list)")
    else:
        me = psutil.Process().pid
        suspects = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            if proc.info["pid"] == me:
                continue
            name = (proc.info["name"] or "").lower()
            cmd = " ".join(proc.info["cmdline"] or [])
            if "python" in name or "meshcore" in name or "claude" in name.lower():
                suspects.append((proc.info["pid"], proc.info["name"], cmd[:70]))
        if suspects:
            print("  processes that commonly hold a radio port:")
            for pid, name, cmd in suspects:
                print(f"    pid {pid:<7} {name:<16} {cmd}")
            print("  close these (or end them in Task Manager) if the port stays busy")
        else:
            print("  no obvious python or MeshCore processes running")

    # The definitive test: can we open it?
    try:
        with serial.Serial(port, 115200, timeout=0.2):
            print(f"  {port} opens fine right now — nothing is holding it")
    except Exception as exc:
        print(f"  {port} will NOT open: {exc}")


def hard_reset(port: str, baud: int) -> None:
    """Pulse DTR/RTS the way an ESP32 flasher does, to reboot the board."""
    print(f"\npulsing the reset line on {port} ...")
    try:
        with serial.Serial(port, baud, timeout=1) as ser:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            # EN low, then release: the standard auto-reset wiring on these boards.
            ser.dtr = False
            ser.rts = True
            time.sleep(0.15)
            ser.rts = False
            time.sleep(0.15)
            ser.dtr = False
    except Exception as exc:
        print(f"  couldn't pulse it: {exc}")
        return
    print("  done — giving the board 3s to come back up")
    time.sleep(3)


async def drain_queue(port: str, baud: int, limit: int, verbose: bool) -> None:
    """Fetch queued messages until the radio says there are no more."""
    from meshcore import MeshCore, EventType

    print(f"\nconnecting to {port} ...")
    mc = None
    for attempt in range(1, 7):
        try:
            mc = await MeshCore.create_serial(port, baud)
            break
        except Exception as exc:
            print(f"  busy (attempt {attempt}/6): {exc}")
            await asyncio.sleep(1.5)
    if mc is None:
        raise SystemExit("could not connect — try --hard, or unplug and replug")

    drained = kinds = 0
    counts: dict[str, int] = {}
    try:
        info = await mc.commands.send_device_query()
        payload = getattr(info, "payload", {})
        if isinstance(payload, dict) and payload.get("model"):
            print(f"  {payload.get('model')} running {payload.get('ver')}")

        print("\ndraining the message queue ...")
        start = time.monotonic()
        while drained < limit:
            result = await mc.commands.get_msg()
            etype = getattr(result, "type", None)
            if etype in (EventType.NO_MORE_MSGS, EventType.ERROR):
                reason = getattr(result, "payload", None)
                if etype == EventType.ERROR:
                    print(f"  stopped on error: {reason!r}")
                break
            drained += 1
            name = getattr(etype, "name", str(etype))
            counts[name] = counts.get(name, 0) + 1
            if verbose:
                print(f"  {drained:4d}  {name}  {getattr(result, 'payload', '')!r:.90}")
            elif drained % 10 == 0:
                print(f"  {drained} ...")
            await asyncio.sleep(0.05)

        elapsed = time.monotonic() - start
        if drained:
            print(f"\ndrained {drained} queued message(s) in {elapsed:.1f}s")
            for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
                print(f"  {n:4d}  {name}")
            if drained >= limit:
                print(f"\nhit the --limit of {limit}; run it again to keep going")
        else:
            print("\nqueue was already empty — the radio is clear")
    finally:
        await mc.disconnect()
        await asyncio.sleep(0.5)
        print("port released")


def clean_partials(folder: Path) -> None:
    leftovers = sorted(folder.glob("*.moss-partial"))
    if not leftovers:
        print(f"\nno .moss-partial files in {folder.resolve()}")
        return
    print(f"\ndeleting {len(leftovers)} partial transfer file(s):")
    for path in leftovers:
        print(f"  {path.name}  ({path.stat().st_size:,} bytes)")
        path.unlink()
    print("note: any interrupted transfer now starts from scratch")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--ports", action="store_true", help="list serial ports and exit")
    ap.add_argument("--who", action="store_true",
                    help="show what might be holding the port, then exit")
    ap.add_argument("--hard", action="store_true",
                    help="pulse the board's reset line before draining")
    ap.add_argument("--limit", type=int, default=500,
                    help="stop after this many queued messages")
    ap.add_argument("--verbose", action="store_true", help="print each message drained")
    ap.add_argument("--clean-partials", action="store_true",
                    help="delete leftover .moss-partial files here")
    ap.add_argument("--dir", default=".", help="folder to clean partials from")
    ap.add_argument("--no-drain", action="store_true",
                    help="skip the queue drain (use with --hard or --clean-partials)")
    args = ap.parse_args()

    if args.ports:
        show_ports()
        return

    if args.clean_partials:
        clean_partials(Path(args.dir))
        if args.no_drain and not args.hard:
            return

    port = args.port or pick_port()

    if args.who:
        who_has_it(port)
        return

    if args.hard:
        hard_reset(port, args.baud)

    if not args.no_drain:
        await drain_queue(port, args.baud, args.limit, args.verbose)

    print("\nradio should be responsive again. If it isn't:")
    print("  python reset_radio.py --who        see what's holding the port")
    print("  python reset_radio.py --hard       reboot the board over serial")
    print("  unplug and replug the radio        always works, COM number may change")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
