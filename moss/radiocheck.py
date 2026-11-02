"""Radio check: can two radios hear each other on #moss?

One side sends numbered chat messages, the other listens and counts them.
They're ordinary short chat messages, so anyone with #moss added sees
them; MOSS's own pages travel as longer binary spores, which drop out a
little sooner at range.

On the Pi the Seed Vault service normally holds the radio, so the check
pauses it first and starts it again afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Callable

from . import service
from .config import load

Out = Callable[[str], None]


def describe(info: dict) -> str:
    """'Heltec V3 · 910.525 MHz · BW 62.5 kHz · SF7 · CR5 · 22 dBm'"""
    parts = []
    if info.get("name"):
        parts.append(str(info["name"]))
    if info.get("radio_freq"):
        parts.append(f"{info['radio_freq']:g} MHz")
    if info.get("radio_bw"):
        parts.append(f"BW {info['radio_bw']:g} kHz")
    if info.get("radio_sf"):
        parts.append(f"SF{info['radio_sf']}")
    if info.get("radio_cr"):
        parts.append(f"CR{info['radio_cr']}")
    if info.get("tx_power"):
        parts.append(f"{info['tx_power']} dBm")
    return " · ".join(parts) or "the radio didn't describe itself"


@dataclass
class Link:
    """How to reach the radio: a serial port, or Bluetooth (ble not None)."""
    port: str | None = None
    ble: str | None = None
    pin: str | None = None

    @classmethod
    def of(cls, link) -> "Link":
        """A Link, a port name, or None (= whatever setup saved)."""
        if isinstance(link, Link):
            return link
        cfg = load()
        if link:
            return cls(port=link)
        return cls(cfg.get("port"), cfg.get("ble"), cfg.get("pin"))


@contextlib.asynccontextmanager
async def radio(link, out: Out, pause_vault: bool | None = None):
    """Open the radio on #moss, pausing the Seed Vault service if it holds the port."""
    from .transport import MeshCoreTransport
    cfg = load()
    link = Link.of(link)
    if pause_vault is None:
        pause_vault = cfg.get("role") == "vault" and service.available() and service.is_active()
    if pause_vault:
        out("pausing the Seed Vault so the check can use the radio...")
        service.control("stop")
        await asyncio.sleep(1.5)
    tx = None
    try:
        tx = await MeshCoreTransport.open(link.port, 115200,
                                          cfg.get("channel") or "#moss", log=out,
                                          scope=cfg.get("scope"), ble=link.ble, pin=link.pin)
        out(f"connected over {tx.where}")
        out(f"radio: {describe(tx.radio_info())}")
        out(f"{tx.channel_name} is in slot {tx.channel}")
        yield tx
    finally:
        if tx is not None:
            await tx.close()
        if pause_vault:
            out("starting the Seed Vault again...")
            service.control("start")


async def send(port, count: int = 10, gap: float = 3.0, out: Out = print) -> int:
    async with radio(port, out) as tx:
        name = tx.radio_info().get("name") or "a MOSS radio"
        sent = 0
        for i in range(1, count + 1):
            ok = await tx.send_text(f"moss ping {i}/{count} from {name}")
            sent += ok
            out(f"  ping {i}/{count} {'sent' if ok else 'REFUSED by the radio'}")
            if i < count:
                await asyncio.sleep(gap)
        out(f"sent {sent} of {count}. On the other radio, see how many arrived.")
        return sent


async def listen(port, seconds: float = 300, out: Out = print,
                 until: int | None = None) -> list[str]:
    heard: list[str] = []
    done = asyncio.Event()

    def on_text(text: str, info: dict) -> None:
        bits = []
        if info.get("SNR") is not None:
            bits.append(f"SNR {info['SNR']:g} dB")
        if info.get("RSSI") is not None:
            bits.append(f"RSSI {info['RSSI']} dBm")
        extra = f"  ({', '.join(bits)})" if bits else ""
        out(f"  [{time.strftime('%H:%M:%S')}] {text}{extra}")
        heard.append(text)
        if until and len(heard) >= until:
            done.set()

    async with radio(port, out) as tx:
        tx.subscribe_text(on_text)
        out(f"listening for {seconds:g}s (Ctrl+C to stop early)... send pings from the other radio now")
        try:
            await asyncio.wait_for(done.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        out(f"heard {len(heard)} message{'s' if len(heard) != 1 else ''}")
        if not heard:
            out("  nothing: out of range, different radio settings, or nobody was sending. "
                "Try the two radios in the same room first; if that works, it's distance.")
        return heard


async def info(port, out: Out = print, pause_vault: bool | None = None) -> dict:
    async with radio(port, out, pause_vault) as tx:
        return {**tx.radio_info(), "slot": tx.channel}


# ---------------------------------------------------------------- a dedicated link

PUBLIC_US = (910.525, 62.5, 7, 5)        # MeshCore's recommended USA/Canada settings
BANDWIDTHS = (7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125.0, 250.0, 500.0)


def check_link(freq: float, bw: float, sf: int, cr: int) -> list[str]:
    """Problems with these settings (empty if fine). Warnings start with 'note:'."""
    problems = []
    if bw not in BANDWIDTHS:
        problems.append(f"bandwidth must be one of {', '.join(f'{b:g}' for b in BANDWIDTHS)} kHz")
    if not 5 <= sf <= 12:
        problems.append("spreading factor must be 5-12")
    if not 5 <= cr <= 8:
        problems.append("coding rate must be 5-8")
    if not 150 <= freq <= 960:
        problems.append("frequency must be in MHz, like 915.0")
    elif not 902 <= freq <= 928:
        problems.append("note: that's outside the US 902-928 MHz band; make sure it's legal where you are")
    return problems


async def set_link(link, freq: float, bw: float, sf: int, cr: int, out: Out = print) -> bool:
    """Put this radio on these settings. Every MOSS radio that should hear it
    needs the same four numbers, and on anything but the public settings it
    no longer hears the public mesh."""
    errors = [p for p in check_link(freq, bw, sf, cr) if not p.startswith("note:")]
    if errors:
        for e in errors:
            out(e)
        return False
    async with radio(link, out) as tx:
        ev = await tx.mc.commands.set_radio(freq, bw, sf, cr)
        ok = getattr(ev, "type", None) != tx._EventType.ERROR
        out(f"radio set to {freq:g} MHz, BW {bw:g} kHz, SF{sf}, CR{cr}" if ok
            else "the radio refused those settings")
        if ok:
            out("some radios only switch after a restart: unplug it (or press reset) if the "
                "radio check still shows the old settings")
        return ok
