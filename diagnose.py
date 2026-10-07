"""Find out why datagrams aren't going out. Prints everything, hides nothing.

    python3 diagnose.py                 # auto-detect port
    python3 diagnose.py --channel 1     # also try one datagram on channel 1
    python3 diagnose.py --channel 1 --debug

Checks, in order:
  1. what firmware this radio is running
  2. which channel slots actually exist on it
  3. whether a plain chat message works (control: proves the link is fine)
  4. whether one binary datagram works, with the full error payload
"""

import argparse
import asyncio
import logging

from meshcore import MeshCore, EventType
from serial.tools import list_ports


def pick_port() -> str:
    ports = [p for p in list_ports.comports()
             if "bluetooth" not in p.description.lower()]
    if not ports:
        raise SystemExit("no serial ports found")
    if len(ports) == 1:
        print(f"using {ports[0].device} ({ports[0].description})\n")
        return ports[0].device
    for p in ports:
        print(f"  {p.device:20} {p.description}")
    raise SystemExit("\npick one with --port")


def dump(label: str, result) -> None:
    """Print an Event's type AND payload — the payload holds the reason."""
    etype = getattr(result, "type", None)
    payload = getattr(result, "payload", None)
    print(f"  {label}")
    print(f"    type:    {etype}")
    print(f"    payload: {payload!r}")


async def step_device_info(mc) -> None:
    print("\n[1] device info")
    for name in ("send_device_query", "get_device_info", "send_appstart"):
        fn = getattr(mc.commands, name, None)
        if fn is None:
            continue
        try:
            dump(name, await fn())
        except Exception as exc:
            print(f"  {name} raised: {exc}")
    info = getattr(mc, "self_info", None)
    if info:
        print(f"  self_info: {info}")


async def step_channels(mc, upto: int = 6) -> None:
    print("\n[2] channel slots")
    for idx in range(upto):
        try:
            result = await mc.commands.get_channel(idx)
            payload = getattr(result, "payload", {})
            if getattr(result, "type", None) == EventType.ERROR:
                print(f"  slot {idx}: error {payload!r}")
                continue
            name = ""
            if isinstance(payload, dict):
                name = (payload.get("channel_name") or payload.get("name") or "")
            print(f"  slot {idx}: {name!r}  {payload!r}")
        except Exception as exc:
            print(f"  slot {idx}: raised {exc}")


async def step_chat_control(mc, channel: int) -> None:
    """If this works and the datagram doesn't, the problem is command 0x3E."""
    print(f"\n[3] control: plain chat message on channel {channel}")
    try:
        dump("send_chan_msg", await mc.commands.send_chan_msg(channel, "moss test"))
    except Exception as exc:
        print(f"  raised: {exc}")


async def step_datagram(mc, channel: int) -> None:
    print(f"\n[4] one binary datagram on channel {channel}")
    payload = b"MOSS" + bytes(range(12))
    for data_type in (0xFF01, 0xFFFF):
        try:
            result = await mc.commands.send_channel_data(channel, data_type, payload)
            dump(f"send_channel_data(chan={channel}, type=0x{data_type:04X}, "
                 f"{len(payload)} bytes)", result)
        except Exception as exc:
            print(f"  type 0x{data_type:04X} raised: {exc}")
        await asyncio.sleep(5)  # let the radio finish transmitting


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--channel", type=int, help="slot to try sending on")
    ap.add_argument("--debug", action="store_true", help="log raw serial frames")
    ap.add_argument("--skip-chat", action="store_true",
                    help="don't send the control chat message")
    args = ap.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG,
                            format="%(name)s %(levelname)s %(message)s")

    port = args.port or pick_port()
    mc = await MeshCore.create_serial(port, args.baud, debug=args.debug)
    print(f"connected to {port}")

    await step_device_info(mc)
    await step_channels(mc)

    if args.channel is not None:
        if not args.skip_chat:
            await step_chat_control(mc, args.channel)
            await asyncio.sleep(5)
        await step_datagram(mc, args.channel)
    else:
        print("\n(pass --channel N to also test sending)")

    print("\ndone")


if __name__ == "__main__":
    asyncio.run(main())
