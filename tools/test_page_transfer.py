"""Run page_transfer's sender and receiver against a fake radio.

No hardware needed. A FakeRadio pair relays datagrams between the two sides
with configurable packet loss, so the interesting cases can actually be
exercised: loss, repair rounds, a receiver that dies, and a sender that dies.

    python test_page_transfer.py
"""

from __future__ import annotations

import asyncio
import random
import sys
import tempfile
import types
from pathlib import Path

# page_transfer imports meshcore and pyserial at module load; stub them so
# this runs on a machine with neither installed.
if "meshcore" not in sys.modules:
    fake = types.ModuleType("meshcore")

    class EventType:
        CHANNEL_DATA_RECV = "channel_data"
        NO_MORE_MSGS = "no_more"
        ERROR = "error"

    fake.EventType = EventType
    fake.MeshCore = object
    sys.modules["meshcore"] = fake
    serial_mod = types.ModuleType("serial")
    tools = types.ModuleType("serial.tools")
    list_ports = types.ModuleType("serial.tools.list_ports")
    list_ports.comports = lambda: []
    tools.list_ports = list_ports
    serial_mod.tools = tools
    sys.modules["serial"] = serial_mod
    sys.modules["serial.tools"] = tools
    sys.modules["serial.tools.list_ports"] = list_ports

import page_transfer as pt
from meshcore import EventType


class Event:
    def __init__(self, payload):
        self.payload = payload


class FakeCommands:
    """Mirrors the real library: commands live on mc.commands, not on mc.

    Keeping this split matters. A flattened mock once hid a bug where a
    wrapper forwarded attributes to the radio but not to its command
    handler, so get_msg vanished only on real hardware.
    """

    def __init__(self, radio: "FakeRadio"):
        self._radio = radio

    async def send_channel_data(self, chan, data_type, payload):
        return await self._radio._send(chan, data_type, payload)

    async def get_msg(self):
        return await self._radio._get_msg()


class FakeRadio:
    """One endpoint. Datagrams go to every other radio on the same air."""

    def __init__(self, air: "Air", name: str):
        self.air, self.name = air, name
        self.inbox: list[bytes] = []
        self.handlers: list = []
        self.commands = FakeCommands(self)
        self.sent = 0
        self.by_type: dict[int, int] = {}

    # --- the two methods page_transfer calls, reached via .commands
    async def _send(self, chan, data_type, payload):
        if len(payload) > pt.MAX_DATAGRAM:
            raise AssertionError(f"{len(payload)} bytes exceeds the datagram limit")
        self.sent += 1
        self.by_type[payload[0]] = self.by_type.get(payload[0], 0) + 1
        self.air.deliver(self, data_type, payload)
        return types.SimpleNamespace(type="ok", payload={})

    async def _get_msg(self):
        if not self.inbox:
            return types.SimpleNamespace(type=EventType.NO_MORE_MSGS, payload={})
        data_type, payload = self.inbox.pop(0)
        event = Event({"data_type": data_type, "payload": payload.hex(),
                       "channel_idx": 1, "SNR": 10.0, "path_len": 255})
        for handler in list(self.handlers):
            await handler(event)
        return types.SimpleNamespace(type="channel_data", payload={})

    def subscribe(self, event_type, handler):
        self.handlers.append(handler)

    async def disconnect(self):
        pass


class Air:
    def __init__(self, loss: float = 0.0, seed: int = 0):
        self.radios: list[FakeRadio] = []
        self.loss, self.rng = loss, random.Random(seed)
        self.dropped = 0

    def radio(self, name: str) -> FakeRadio:
        r = FakeRadio(self, name)
        self.radios.append(r)
        return r

    def deliver(self, source: FakeRadio, data_type: int, payload: bytes) -> None:
        for radio in self.radios:
            if radio is source:
                continue
            if self.rng.random() < self.loss:
                self.dropped += 1
                continue
            radio.inbox.append((data_type, payload))


def quiet_progress():
    """Silence the progress bars so test output stays readable."""
    pt.Progress.__init__ = lambda self, total, label, quiet=True: (
        setattr(self, "total", total), setattr(self, "n", 0),
        setattr(self, "bar", None), setattr(self, "quiet", True),
        setattr(self, "label", label), setattr(self, "start", 0.0), None)[-1]
    pt.Progress.update = lambda self, n=1, note="": None
    pt.Progress.close = lambda self: None
    pt.Progress.write = lambda self, text: None


async def run_pair(air, tx, rx, source: Path, out_dir: Path, **kwargs):
    sender = pt.send_page(tx, 1, source, kwargs.get("gap", 0.01), 4,
                          pt.MOSS_DATA_TYPE, kwargs.get("zlib", True),
                          wait=kwargs.get("wait", 3.0),
                          handshake=kwargs.get("handshake", 2.0))
    receiver = pt.recv_page(rx, 1, None, out_dir, seconds=10,
                            quiet=kwargs.get("quiet", 0.5),
                            data_type=pt.MOSS_DATA_TYPE,
                            idle=kwargs.get("idle", 6.0))
    return await asyncio.gather(receiver, sender, return_exceptions=True)


async def test_clean(tmp: Path, source: Path) -> None:
    air = Air(loss=0.0)
    tx, rx = air.radio("tx"), air.radio("rx")
    await run_pair(air, tx, rx, source, tmp)
    got = tmp / source.name
    assert got.exists(), "no file written"
    assert got.read_bytes() == source.read_bytes(), "content differs"
    print(f"  clean link           {tx.sent + rx.sent} datagrams, file matches")


async def test_lossy(tmp: Path, source: Path) -> None:
    for loss in (0.2, 0.4):
        for f in tmp.glob("*"):
            f.unlink()
        air = Air(loss=loss, seed=11)
        tx, rx = air.radio("tx"), air.radio("rx")
        await run_pair(air, tx, rx, source, tmp)
        got = tmp / source.name
        assert got.exists() and got.read_bytes() == source.read_bytes(), \
            f"failed at {loss:.0%} loss"
        print(f"  {loss:.0%} loss             {tx.sent + rx.sent} datagrams, "
              f"{air.dropped} dropped, file matches")


async def test_receiver_dies(tmp: Path, source: Path) -> None:
    """Receiver is killed mid-transfer; its partial file must survive."""
    for f in tmp.glob("*"):
        f.unlink()
    air = Air(loss=0.0)
    tx, rx = air.radio("tx"), air.radio("rx")

    receiver = asyncio.create_task(
        pt.recv_page(rx, 1, None, tmp, 10, 0.5, pt.MOSS_DATA_TYPE, idle=6.0))
    sender = asyncio.create_task(
        pt.send_page(tx, 1, source, 0.35, 4, pt.MOSS_DATA_TYPE, True, 3.0, 0.5))
    await asyncio.sleep(3.0)                      # handshake, then some chunks
    receiver.cancel()                             # the Ctrl-C case
    try:
        await receiver
    except asyncio.CancelledError:
        pass
    sender.cancel()
    try:
        await sender
    except asyncio.CancelledError:
        pass

    partials = list(tmp.glob("*.moss-partial"))
    assert partials, "nothing was kept when the receiver was cancelled"
    import json
    held = len(json.loads(partials[0].read_text())["chunks"])
    assert held > 0
    print(f"  receiver killed      kept {held} chunk(s) in {partials[0].name}")
    return held


async def test_sender_dies_then_resumes(tmp: Path, source: Path, held: int) -> None:
    """A FRESH sender (new transfer id, as after a power cut) must send only
    what the receiver still needs."""
    air = Air(loss=0.0)
    tx, rx = air.radio("tx"), air.radio("rx")
    await run_pair(air, tx, rx, source, tmp, gap=0.01)

    got = tmp / source.name
    assert got.exists() and got.read_bytes() == source.read_bytes(), \
        "resumed transfer produced the wrong file"

    blob, _ = pt.pack(source.read_bytes(), True)
    total = max(1, -(-len(blob) // pt.CHUNK_BYTES))
    chunk_datagrams = tx.by_type.get(pt.CHUNK, 0)
    print(f"  sender restarted     resent {chunk_datagrams} of {total} chunks "
          f"(receiver already had {held})")
    assert chunk_datagrams < total, "resume sent everything again"
    assert not list(tmp.glob("*.moss-partial")), "partial file not cleaned up"


async def test_no_runaway_nack(tmp: Path, source: Path) -> None:
    """With the sender gone, the receiver must stop transmitting."""
    for f in tmp.glob("*"):
        f.unlink()
    air = Air(loss=0.0)
    tx, rx = air.radio("tx"), air.radio("rx")

    receiver = asyncio.create_task(
        pt.recv_page(rx, 1, None, tmp, 10, 0.3, pt.MOSS_DATA_TYPE, idle=3.0))
    # Announce a page, send one chunk, then vanish.
    blob, flags = pt.pack(source.read_bytes(), True)
    chunks = [blob[i:i + pt.CHUNK_BYTES] for i in range(0, len(blob), pt.CHUNK_BYTES)]
    tid = 4321
    await tx.commands.send_channel_data(1, pt.MOSS_DATA_TYPE, pt.build_manifest(
        tid, len(chunks), len(blob), pt.short_hash(source.read_bytes()),
        source.name, flags))
    await asyncio.sleep(0.3)
    await tx.commands.send_channel_data(1, pt.MOSS_DATA_TYPE,
                                        pt.build_chunk(tid, 0, chunks[0]))

    await asyncio.wait_for(receiver, timeout=12)
    print(f"  dead sender          receiver stopped on its own after "
          f"{rx.sent} transmissions")
    assert rx.sent < 8, f"receiver kept NACKing ({rx.sent} transmissions)"


async def main() -> None:
    quiet_progress()
    # A folder beside this script, so it works on Windows, macOS and Linux.
    tmp = Path(tempfile.gettempdir()) / "moss_transfer_test"
    tmp.mkdir(parents=True, exist_ok=True)
    for f in tmp.glob("*"):
        f.unlink()

    examples = Path(__file__).resolve().parent.parent / "TestExamples"
    source = examples / "Shrubbies.md"
    if not source.exists():
        source = examples / "sample_page.md"
    print(f"testing with {source.name} ({len(source.read_bytes())} bytes)\n")

    await test_clean(tmp, source)
    await test_lossy(tmp, source)
    held = await test_receiver_dies(tmp, source)
    await test_sender_dies_then_resumes(tmp, source, held)
    await test_no_runaway_nack(tmp, source)
    print("\nall checks passed")


if __name__ == "__main__":
    asyncio.run(main())