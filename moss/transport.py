"""Where datagrams come from and go to.

The gateway and client only ever see this tiny interface, so the same code
runs over a real MeshCore radio or over a simulated lossy air for testing.

    await tx.send(datagram)          put one datagram on the channel
    tx.subscribe(callback)           callback(raw_bytes) for every datagram heard
    await tx.close()
"""

from __future__ import annotations

import asyncio
import inspect
import random
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from .protocol import MAX_DATAGRAM, MOSS_DATA_TYPE

Handler = Callable[[bytes], "Awaitable[None] | None"]


class Transport(Protocol):
    async def send(self, datagram: bytes) -> None: ...
    def subscribe(self, handler: Handler) -> None: ...
    async def close(self) -> None: ...


async def _call(handler: Handler, raw: bytes) -> None:
    result = handler(raw)
    if inspect.isawaitable(result):
        await result


# ---------------------------------------------------------------- simulator

class SimNetwork:
    """A shared channel. Every endpoint hears every other endpoint, except
    for the datagrams the 'air' drops.

    loss      probability a datagram is lost on its way to a given listener
    latency   seconds between send and delivery
    airtime   seconds a send blocks, to mimic a radio that can only talk so fast
    seed      makes a lossy run reproducible
    """

    def __init__(self, loss: float = 0.0, latency: float = 0.0,
                 airtime: float = 0.0, seed: int | None = None):
        self.loss, self.latency, self.airtime = loss, latency, airtime
        self.rng = random.Random(seed)
        self.endpoints: list[SimTransport] = []
        self.sent = 0
        self.dropped = 0

    def endpoint(self, name: str = "") -> "SimTransport":
        ep = SimTransport(self, name)
        self.endpoints.append(ep)
        return ep


class SimTransport:
    def __init__(self, net: SimNetwork, name: str):
        self.net, self.name = net, name
        self.handlers: list[Handler] = []
        self.closed = False
        self.drop_next: list = []      # predicates for deterministic loss in tests

    async def send(self, datagram: bytes) -> None:
        if len(datagram) > MAX_DATAGRAM:
            raise ValueError(f"datagram is {len(datagram)} bytes, max {MAX_DATAGRAM}")
        net = self.net
        net.sent += 1
        if net.airtime:
            await asyncio.sleep(net.airtime)
        for other in net.endpoints:
            if other is self or other.closed:
                continue
            if net.rng.random() < net.loss or other._should_drop(datagram):
                net.dropped += 1
                continue
            asyncio.get_running_loop().call_later(
                net.latency, lambda o=other: asyncio.ensure_future(o._deliver(datagram)))

    def _should_drop(self, raw: bytes) -> bool:
        for pred in list(self.drop_next):
            if pred(raw):
                self.drop_next.remove(pred)
                return True
        return False

    async def _deliver(self, raw: bytes) -> None:
        if self.closed:
            return
        for h in list(self.handlers):
            await _call(h, raw)

    def subscribe(self, handler: Handler) -> None:
        self.handlers.append(handler)

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------- MeshCore radio

class MeshCoreTransport:
    """A real radio that survives the laptop sleeping and the cable coming out.

    MeshCore's own reconnect gives up after three quick tries, and after a
    Windows sleep the port often just goes silent instead of reporting a
    disconnect. So this watches the link itself: a disconnect event, a failed
    write, or several polls in a row with no answer all count as "lost". It
    then releases the old port handle (otherwise Windows says "Access is
    denied") and keeps reopening until the radio is back, re-picking the
    port if it was auto-detected and the COM number changed.

    While the link is down, datagrams are dropped as if lost in the air; the
    transfer protocol already repeats what didn't arrive.

    MOSS talks on exactly one channel, #moss by default, found or created on
    the radio at every connect (see channel.py). Data arriving on any other
    channel is ignored. An optional flood *scope* (a MeshCore region name)
    keeps repeaters outside that region from carrying MOSS traffic.
    """

    DEAD_AFTER = 3            # unanswered polls in a row before the link counts as lost
    POLL_TIMEOUT = 4.0        # seconds to wait for the radio to answer a poll
    SEND_WAIT = 20.0          # how long a send waits for a reconnect before giving up
    RETRY_DELAY = 2.0         # first pause between reopen attempts (grows to 10 s)

    def __init__(self, port: str, baud: int, channel_name: str = "#moss",
                 data_type: int = MOSS_DATA_TYPE, poll_interval: float = 1.0,
                 auto_port: bool = False, log: Callable[[str], None] | None = None,
                 scope: str | None = None):
        self.port, self.baud, self.data_type = port, baud, data_type
        self.channel_name, self.scope = channel_name, scope
        self.channel: int | None = None     # the slot #moss lives in on this radio
        self.scoped = False
        self.auto_port = auto_port
        self.handlers: list[Handler] = []
        self.mc = None
        self.state = "connecting"            # connecting, connected, reconnecting, closed
        self.reconnects = 0
        self._poll_interval = poll_interval
        self._up = asyncio.Event()
        self._lost = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.log = log or (lambda m: None)

    @classmethod
    async def open(cls, port: str | None, baud: int, channel_name: str = "#moss",
                   data_type: int = MOSS_DATA_TYPE,
                   log: Callable[[str], None] | None = None,
                   scope: str | None = None) -> "MeshCoreTransport":
        from .channel import check_name
        check_name(channel_name)
        auto = port is None
        port = port or pick_port()
        if port.startswith("/") and not Path(port).exists():
            raise SystemExit(f"no such port: {port}\n{_port_listing()}")
        tx = cls(port, baud, channel_name, data_type, auto_port=auto, log=log, scope=scope)
        last = None
        for attempt in range(1, 7):
            try:
                await tx._attach()
                break
            except Exception as exc:          # port often still held by a prior run
                last = exc
                text = str(exc).lower()
                if not any(k in text for k in ("access is denied", "permission",
                                               "could not open port", "in use", "no answer")):
                    raise
                await asyncio.sleep(1.5)
        else:
            raise SystemExit(f"{port} stayed busy. Close other terminals or the "
                             f"MeshCore app. Last error: {last}")
        tx._task = asyncio.create_task(tx._supervise())
        return tx

    # ------------------------------------------------------------ connection
    async def _attach(self) -> None:
        """Open the port and start listening."""
        from meshcore import EventType, MeshCore
        from .channel import ChannelError, find_or_create
        mc = await MeshCore.create_serial(self.port, self.baud)
        if mc is None or not getattr(mc, "is_connected", True):
            raise ConnectionError("no answer from the radio")
        self._EventType = EventType
        try:
            self.channel, _ = await find_or_create(mc, self.channel_name, EventType, self.log)
        except ChannelError as exc:
            try:
                await mc.disconnect()
            finally:
                raise SystemExit(str(exc))
        if self.scope:
            ev = await mc.commands.set_flood_scope(self.scope)
            self.scoped = getattr(ev, "type", None) != EventType.ERROR
            if not self.scoped:
                self.log(f"this radio's firmware didn't accept the scope {self.scope!r}; "
                         f"sending unscoped (needs MeshCore 1.12+)")
        mc.subscribe(EventType.CHANNEL_DATA_RECV, self._on_event)
        mc.subscribe(EventType.DISCONNECTED, self._on_disconnect)
        self.mc = mc
        self._lost.clear()
        self.state = "connected"
        self._up.set()

    async def _detach(self) -> None:
        """Release the old handle, even if the device is already gone."""
        mc, self.mc = self.mc, None
        self._up.clear()
        if mc is not None:
            try:
                await asyncio.wait_for(mc.disconnect(), 3)
            except Exception:
                pass
        await asyncio.sleep(0.3)

    async def _on_disconnect(self, event) -> None:
        self._lost.set()

    async def _supervise(self) -> None:
        while True:
            await self._poll_until_lost()
            if self.state == "closed":
                return
            self.state = "reconnecting"
            self.log("radio link lost; reconnecting (sleep, unplugged cable, or a busy port)")
            await self._detach()
            delay = self.RETRY_DELAY
            while self.state != "closed":
                try:
                    if self.auto_port:
                        try:
                            self.port = pick_port()
                        except SystemExit:
                            raise ConnectionError("radio not plugged in")
                    await self._attach()
                    self.reconnects += 1
                    self.log(f"radio back on {self.port}")
                    break
                except asyncio.CancelledError:
                    raise
                except SystemExit as exc:            # e.g. no free channel slot
                    self.log(str(exc))
                    await self._detach()
                    await asyncio.sleep(delay)
                    delay = min(delay * 1.5, 10.0)
                except Exception:
                    await self._detach()
                    await asyncio.sleep(delay)
                    delay = min(delay * 1.5, 10.0)

    async def _poll_until_lost(self) -> None:
        """The radio queues messages; nothing arrives unless we ask for them."""
        misses = 0
        while not self._lost.is_set():
            mc = self.mc
            if mc is None or not getattr(mc, "is_connected", True):
                return
            try:
                result = await mc.commands.get_msg(timeout=self.POLL_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception:
                misses += 1
                if misses >= self.DEAD_AFTER:
                    return
                await asyncio.sleep(self._poll_interval)
                continue
            kind = getattr(result, "type", None)
            payload = getattr(result, "payload", None)
            if kind == self._EventType.ERROR and isinstance(payload, dict) \
                    and payload.get("reason") == "no_event_received":
                misses += 1                   # the radio didn't answer at all
                if misses >= self.DEAD_AFTER:
                    return
                continue
            misses = 0
            idle = kind in (self._EventType.NO_MORE_MSGS, self._EventType.ERROR)
            await asyncio.sleep(self._poll_interval if idle else 0.05)

    # ------------------------------------------------------------ the Transport interface
    async def send(self, datagram: bytes) -> None:
        if not self._up.is_set():
            try:
                await asyncio.wait_for(self._up.wait(), self.SEND_WAIT)
            except asyncio.TimeoutError:
                return                        # lost in the air; the protocol repeats it
        try:
            await self.mc.commands.send_channel_data(self.channel, self.data_type, datagram)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._lost.set()                  # a failed write means the link is gone

    def subscribe(self, handler: Handler) -> None:
        self.handlers.append(handler)

    async def _on_event(self, event) -> None:
        p = event.payload if isinstance(event.payload, dict) else {}
        if p.get("data_type") != self.data_type:
            return
        if "channel_idx" in p and p["channel_idx"] != self.channel:
            return                            # MOSS only listens on its own channel
        raw = p.get("payload")
        if isinstance(raw, str):
            try:
                raw = bytes.fromhex(raw)
            except ValueError:
                return
        if not isinstance(raw, (bytes, bytearray)):
            return
        for h in list(self.handlers):
            await _call(h, bytes(raw))

    async def close(self) -> None:
        self.state = "closed"
        self._lost.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self._detach()


def _port_listing() -> str:
    """What's plugged in, with the stable /dev/serial/by-id names on Linux."""
    lines = []
    by_id = Path("/dev/serial/by-id")
    if by_id.is_dir():
        for link in sorted(by_id.iterdir()):
            lines.append(f"  {link}   (= {link.resolve()})")
    if not lines:
        from serial.tools import list_ports
        lines = [f"  {p.device:20} {p.description}" for p in list_ports.comports()]
    return ("serial ports found:\n" + "\n".join(lines)) if lines else "no serial ports found"


def pick_port() -> str:
    from serial.tools import list_ports
    ports = [p for p in list_ports.comports()
             if "bluetooth" not in p.description.lower()]
    if not ports:
        raise SystemExit("no serial ports found")
    if len(ports) == 1:
        return ports[0].device
    listing = "\n".join(f"  {p.device:20} {p.description}" for p in ports)
    raise SystemExit(f"several serial ports found, pick one with --port:\n{listing}")
