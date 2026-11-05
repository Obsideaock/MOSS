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
import sys
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

    async def send(self, datagram: bytes, path: bytes | None = None) -> None:
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

    Bluetooth works the same way (ble="" finds any MeshCore radio nearby;
    ble="<address or name>" a particular one): same commands, same watchdog,
    just a different link to the radio.

    Routing (opt-in, route=True): MeshCore floods channel messages through
    every repeater. With routing on, MOSS watches the radio's packet log for
    the route each flooded #moss message took (decrypting it with the
    channel's public key to match it to the MOSS datagram inside), and a
    reply can then be sent back along that route only: send(raw, path=...).
    No route known, or a route with multi-byte repeater ids: it floods as
    always. Uses only stock companion firmware features.

    MOSS talks on exactly one channel, #moss by default, found or created on
    the radio at every connect (see channel.py). Data arriving on any other
    channel is ignored. An optional flood *scope* (a MeshCore region name)
    keeps repeaters outside that region from carrying MOSS traffic.
    """

    DEAD_AFTER = 3            # unanswered polls in a row before the link counts as lost
    POLL_TIMEOUT = 4.0        # seconds to wait for the radio to answer a poll
    SEND_WAIT = 20.0          # how long a send waits for a reconnect before giving up
    RETRY_DELAY = 2.0         # first pause between reopen attempts (grows to 10 s)
    SEND_TRIES = 3            # attempts per datagram when the radio refuses it
    STUCK_AFTER = 6           # datagrams in a row the radio refused: reconnect

    def __init__(self, port: str, baud: int, channel_name: str = "#moss",
                 data_type: int = MOSS_DATA_TYPE, poll_interval: float = 1.0,
                 auto_port: bool = False, log: Callable[[str], None] | None = None,
                 scope: str | None = None, ble: str | None = None, pin: str | None = None,
                 route: bool = False):
        self.port, self.baud, self.data_type = port, baud, data_type
        self.route = route
        self._heard_log: list[tuple[bytes, bytes]] = []      # (decrypted #moss packet, route) recent
        self._routes: dict[bytes, bytes] = {}                # datagram digest -> route back
        self.routed_sends = 0
        self.ble, self.pin = ble, pin          # ble None: USB serial
        self.radio_name = ""
        self.channel_name, self.scope = channel_name, scope
        self.channel: int | None = None     # the slot #moss lives in on this radio
        self.scoped = False
        self.auto_port = auto_port
        self.handlers: list[Handler] = []
        self.text_handlers: list = []         # plain chat messages on #moss (radio check)
        self.mc = None
        self.state = "connecting"            # connecting, connected, reconnecting, closed
        self.reconnects = 0
        self._poll_interval = poll_interval
        self._up = asyncio.Event()
        self._lost = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.log = log or (lambda m: None)
        # One command at a time on the serial link, so a send's answer can't be
        # mistaken for a poll's (both listen for the radio's error reply).
        self._cmd = asyncio.Lock()
        self.refused = 0                      # datagrams the radio refused to send
        self._refused_streak = 0
        self.lost_reason = ""

    @property
    def where(self) -> str:
        """How this radio is reached, for messages: COM12, or Bluetooth MeshCore-Stern."""
        if self.ble is None:
            return self.port or "?"
        return f"Bluetooth ({self.radio_name or self.ble or 'any MeshCore radio'})"

    @classmethod
    async def open(cls, port: str | None, baud: int, channel_name: str = "#moss",
                   data_type: int = MOSS_DATA_TYPE,
                   log: Callable[[str], None] | None = None,
                   scope: str | None = None, ble: str | None = None,
                   pin: str | None = None, route: bool = False) -> "MeshCoreTransport":
        from .channel import check_name
        check_name(channel_name)
        if ble is not None:
            tx = await cls._open_ble(baud, channel_name, data_type, log, scope, ble, pin)
            tx.route = route
            return tx
        auto = port is None
        port = port or pick_port()
        if port.startswith("/") and not Path(port).exists():
            raise SystemExit(f"no such port: {port}\n{_port_listing()}")
        tx = cls(port, baud, channel_name, data_type, auto_port=auto, log=log, scope=scope,
                 route=route)
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

    @classmethod
    async def _open_ble(cls, baud, channel_name, data_type, log, scope, ble, pin):
        """Connect over Bluetooth, retrying a little; a stale pairing fails at once."""
        tx = cls(None, baud, channel_name, data_type, log=log, scope=scope, ble=ble, pin=pin)
        last = None
        for attempt in range(1, 4):           # Bluetooth is flakier to set up than a cable
            try:
                await tx._attach()
                break
            except ImportError as exc:
                raise SystemExit("Bluetooth needs the 'bleak' package: pip install bleak "
                                 "(or run the installer again)") from exc
            except SystemExit:
                raise
            except Exception as exc:
                last = exc
                if is_stale_pairing(exc):       # retrying can't help: the old pairing is the problem
                    raise StalePairing(stale_pairing_help(ble, exc)) from exc
                await asyncio.sleep(2.0)
        else:
            who = f" {ble}" if ble else ""
            if _system() == "darwin":
                pin_help = ("  The first time, your Mac shows its own window asking for the PIN "
                            "(it's in the MeshCore app's settings, often 123456).")
            else:
                pin_help = ("  If your computer asks for a PIN, it's shown in the MeshCore app's settings "
                            "(often 123456); pairing it once in the computer's Bluetooth settings also works.")
            raise SystemExit(
                f"couldn't reach the radio{who} over Bluetooth: {last or 'not found'}\n"
                f"  Is it switched on and nearby? Is the MeshCore phone app disconnected from it "
                f"(a radio takes one Bluetooth connection at a time)?\n" + pin_help)
        tx._task = asyncio.create_task(tx._supervise())
        return tx

    # ------------------------------------------------------------ connection
    async def _attach(self) -> None:
        """Open the port (or Bluetooth link) and start listening."""
        from meshcore import EventType, MeshCore
        from .channel import ChannelError, find_or_create
        if self.ble is not None:
            from . import blepair
            if self.pin and self.ble and blepair.supported():
                # meshcore never hands the PIN to the system, and bleak on Windows can't
                # use one, so pair here first (does nothing if already paired).
                await blepair.pair(self.ble, self.pin)
            # Never pass the PIN to meshcore: it only makes the library call a pairing
            # that macOS doesn't support (the connection then fails) and that can't use
            # a PIN on Windows anyway. Pairing with a PIN is done above, or by the system.
            mc = await MeshCore.create_ble(address=self.ble or None)
            if mc is None or not getattr(mc, "is_connected", True):
                raise ConnectionError("no MeshCore radio answered over Bluetooth")
        else:
            mc = await MeshCore.create_serial(self.port, self.baud)
            if mc is None or not getattr(mc, "is_connected", True):
                raise ConnectionError("no answer from the radio")
        await _ensure_self_info(mc, self.log)
        info = getattr(mc, "self_info", None)
        self.radio_name = (info or {}).get("name", "") if isinstance(info, dict) else ""
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
        text_kind = getattr(EventType, "CHANNEL_MSG_RECV", None)
        if text_kind is not None:
            mc.subscribe(text_kind, self._on_text)
        log_kind = getattr(EventType, "RX_LOG_DATA", None)
        if log_kind is not None:
            from .channel import hashtag_secret
            self._secret = hashtag_secret(self.channel_name)
            mc.subscribe(log_kind, self._on_rx_log)
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

    def _lose(self, reason: str) -> None:
        if not self._lost.is_set():
            self.lost_reason = reason
        self._lost.set()

    async def _on_disconnect(self, event) -> None:
        p = getattr(event, "payload", None)
        why = p.get("reason") if isinstance(p, dict) else None
        self._lose(f"the radio's connection closed ({why or 'disconnect'})")

    async def _alive(self) -> bool:
        """Polls can go unanswered on a healthy radio: the library can't parse
        some message types (newer firmware, other people's traffic), and a
        poll for one of those simply times out. So before declaring the link
        dead, ask the radio something that always gets a reply."""
        mc = self.mc
        if mc is None:
            return False
        try:
            async with self._cmd:
                ev = await asyncio.wait_for(mc.commands.send_device_query(), self.POLL_TIMEOUT + 2)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return getattr(ev, "type", None) not in (None, self._EventType.ERROR)

    async def _supervise(self) -> None:
        while True:
            await self._poll_until_lost()
            if self.state == "closed":
                return
            self.state = "reconnecting"
            self.log(f"radio link lost: {self.lost_reason or 'unknown reason'}. Reconnecting...")
            await self._detach()
            delay = self.RETRY_DELAY
            tries = 0
            while self.state != "closed":
                tries += 1
                try:
                    if self.auto_port and self.ble is None:
                        try:
                            self.port = pick_port()
                        except SystemExit:
                            raise ConnectionError("radio not plugged in")
                    await self._attach()
                    self.reconnects += 1
                    self.log(f"radio back on {self.where}")
                    break
                except asyncio.CancelledError:
                    raise
                except SystemExit as exc:            # e.g. no free channel slot
                    self.log(str(exc))
                    await self._detach()
                    await asyncio.sleep(delay)
                    delay = min(delay * 1.5, 10.0)
                except Exception as exc:
                    if tries == 1 or tries % 5 == 0:
                        self.log(f"still can't reopen {self.where} (try {tries}): "
                                 f"{type(exc).__name__}: {exc}")
                    await self._detach()
                    await asyncio.sleep(delay)
                    delay = min(delay * 1.5, 10.0)

    async def _poll_until_lost(self) -> None:
        """The radio queues messages; nothing arrives unless we ask for them."""
        misses = 0
        while not self._lost.is_set():
            mc = self.mc
            if mc is None or not getattr(mc, "is_connected", True):
                self._lose("the serial connection closed")
                return
            try:
                async with self._cmd:
                    result = await mc.commands.get_msg(timeout=self.POLL_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                misses += 1
                if misses >= self.DEAD_AFTER:
                    if await self._alive():
                        misses = 0
                        continue
                    self._lose(f"polling the radio failed {misses} times ({type(exc).__name__}: {exc})")
                    return
                await asyncio.sleep(self._poll_interval)
                continue
            kind = getattr(result, "type", None)
            payload = getattr(result, "payload", None)
            if kind == self._EventType.ERROR and isinstance(payload, dict) \
                    and payload.get("reason") == "no_event_received":
                misses += 1                   # no answer we could read
                if misses >= self.DEAD_AFTER:
                    if await self._alive():   # it answers other questions: it's fine
                        misses = 0
                        await asyncio.sleep(self._poll_interval)
                        continue
                    self._lose(f"the radio stopped answering ({misses} polls and a status check)")
                    return
                continue
            misses = 0
            idle = kind in (self._EventType.NO_MORE_MSGS, self._EventType.ERROR)
            await asyncio.sleep(self._poll_interval if idle else 0.05)

    # ------------------------------------------------------------ the Transport interface
    # ------------------------------------------------------------ routing (opt-in)
    async def _on_rx_log(self, event) -> None:
        """Every packet the radio hears shows up in its log with the route it
        took. Keep the routes of flooded #moss packets, decrypted, so the
        datagram that arrives a moment later can be matched to its route."""
        if not self.route:
            return
        p = event.payload if isinstance(event.payload, dict) else {}
        try:
            if p.get("payload_type") != 6 or p.get("route_type") not in (0, 1):    # GRP_DATA, flooded
                return
            if p.get("path_hash_size", 1) != 1:
                return                                    # multi-byte repeater ids: just flood
            body = p.get("pkt_payload") or b""
            if isinstance(body, str):
                body = bytes.fromhex(body)
            plain = _open_group(self._secret, body)
            if plain is None:
                return                                    # another channel
            path = bytes.fromhex(p.get("path") or "")
            self._heard_log.append((plain, path))
            del self._heard_log[:-64]
        except (ValueError, TypeError, AttributeError):
            return

    def _remember_route(self, raw: bytes) -> None:
        for plain, path in reversed(self._heard_log):
            if raw in plain:
                key = _digest(raw)
                self._routes[key] = bytes(reversed(path))   # the way back is the way here, reversed
                while len(self._routes) > 256:
                    self._routes.pop(next(iter(self._routes)))
                return

    def return_path(self, raw: bytes) -> bytes | None:
        """The route back to whoever sent this datagram, if routing is on and
        it was learned; None means flood."""
        return self._routes.get(_digest(raw)) if self.route else None

    async def send(self, datagram: bytes, path: bytes | None = None) -> None:
        """Hand one datagram to the radio. The radio can *refuse* (its queue is
        full, or it's busy): that comes back as an error reply, not an
        exception, and used to be silently ignored, so a reader could keep
        asking for a lost capsule without a single ask leaving the laptop.
        Now a refusal is retried, logged, and if the radio keeps refusing,
        the link is treated as stuck and reopened."""
        reason = None
        for attempt in range(1, self.SEND_TRIES + 1):
            if not self._up.is_set():
                try:
                    await asyncio.wait_for(self._up.wait(), self.SEND_WAIT)
                except asyncio.TimeoutError:
                    return                    # lost in the air; the protocol repeats it
            routed = path is not None and self.route
            try:
                async with self._cmd:
                    if routed:
                        ev = await self.mc.commands.send_channel_data(self.channel, self.data_type,
                                                                      datagram, path=path)
                    else:
                        ev = await self.mc.commands.send_channel_data(self.channel, self.data_type,
                                                                      datagram)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._lose(f"writing to the radio failed ({type(exc).__name__}: {exc})")
                return
            if getattr(ev, "type", None) != self._EventType.ERROR:
                self._refused_streak = 0
                self.routed_sends += routed
                return
            if routed:
                path = None                   # the radio didn't like the route: flood instead
            reason = getattr(ev, "payload", None)
            await asyncio.sleep(0.4 * attempt)
        self.refused += 1
        self._refused_streak += 1
        self.log(f"the radio refused to send a message {self.SEND_TRIES} times ({reason}); "
                 f"it counts as lost")
        if self._refused_streak >= self.STUCK_AFTER:
            self._refused_streak = 0
            self._lose(f"the radio refused {self.STUCK_AFTER} messages in a row")

    def subscribe(self, handler: Handler) -> None:
        self.handlers.append(handler)

    # ------------------------------------------------------------ plain chat (radio check only)
    def subscribe_text(self, handler) -> None:
        """handler(text, info) for ordinary chat messages heard on #moss;
        info may hold SNR and RSSI."""
        self.text_handlers.append(handler)

    async def _on_text(self, event) -> None:
        p = event.payload if isinstance(event.payload, dict) else {}
        if p.get("channel_idx", self.channel) != self.channel:
            return
        for h in list(self.text_handlers):
            result = h(p.get("text", ""), p)
            if inspect.isawaitable(result):
                await result

    async def send_text(self, text: str) -> bool:
        """Send an ordinary chat message on #moss. True if the radio took it."""
        if not self._up.is_set():
            try:
                await asyncio.wait_for(self._up.wait(), self.SEND_WAIT)
            except asyncio.TimeoutError:
                return False
        async with self._cmd:
            ev = await self.mc.commands.send_chan_msg(self.channel, text)
        return getattr(ev, "type", None) != self._EventType.ERROR

    def radio_info(self) -> dict:
        """What the radio said about itself: name, frequency, SF, BW, CR, power."""
        mc = self.mc
        info = getattr(mc, "self_info", None) if mc is not None else None
        return dict(info) if isinstance(info, dict) else {}

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
        if self.route:
            self._remember_route(bytes(raw))
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


BLE_UART_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"     # MeshCore's companion service


async def _ensure_self_info(mc, log) -> None:
    """Make sure we have the radio's description (name, frequency, SF, ...).

    The library asks once while connecting and waits only 2 seconds. Over
    Bluetooth, especially straight after pairing or on ESP32 radios like the
    T-Beam, the answer can come later than that: the link works, but the
    radio's settings are missing. Ask again, more patiently.
    """
    info = getattr(mc, "self_info", None)
    if isinstance(info, dict) and info.get("radio_freq"):
        return
    ask = getattr(getattr(mc, "commands", None), "send_appstart", None)
    if ask is None:
        return
    for wait in (5, 8):
        try:
            await ask(timeout=wait)
        except Exception as exc:              # never fatal: MOSS works without the description
            log(f"asking the radio to describe itself failed: {exc}")
            return
        info = getattr(mc, "self_info", None)
        if isinstance(info, dict) and info.get("radio_freq"):
            return
    log("the radio didn't describe itself (name, frequency...); everything else still works")


def _system() -> str:
    from .blepair import system
    return system()


class StalePairing(SystemExit):
    """The computer remembers a Bluetooth pairing the radio has forgotten."""


# How each system reports "my saved pairing keys don't work any more".
_STALE_SIGNS = (
    "peer removed pairing",              # macOS: CBErrorDomain Code=14
    "encryption is insufficient",        # macOS: CBATTErrorDomain Code=15
    "insufficient encryption",
    "insufficient authentication",       # Windows / BlueZ ATT errors
    "authentication failed",             # BlueZ: org.bluez.Error.AuthenticationFailed
    "authenticationfailed",
)


def is_stale_pairing(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(sign in text for sign in _STALE_SIGNS)


def stale_pairing_help(address: str | None, exc: BaseException | None = None,
                       platform: str | None = None) -> str:
    """What to do when the computer and the radio disagree about being paired."""
    platform = platform or _system()
    who = address or "the radio"
    if platform == "darwin":
        steps = ("System Settings → Bluetooth, hover over the MeshCore radio, click ⓘ, "
                 "then Forget This Device")
    elif platform.startswith("win"):
        steps = ("Settings → Bluetooth & devices → Devices, find the MeshCore radio, "
                 "click ⋯ and choose Remove device")
    else:
        steps = "run  bluetoothctl remove " + (address or "<the radio's address>")
    return (f"couldn't reach {who} over Bluetooth: this computer remembers an old pairing "
            f"with the radio, but the radio has forgotten it (this happens after the radio is "
            f"reflashed, reset, or paired with another computer)"
            + (f".\n  ({exc})" if exc else ".") +
            f"\n  Fix: forget the radio on this computer ({steps}), then try again. "
            f"You'll be asked for the PIN: it's in the MeshCore app's settings, often 123456.")


async def scan_ble(seconds: float = 6.0) -> list[tuple[str, str, int | None]]:
    """MeshCore radios advertising over Bluetooth nearby: (address, name, signal dBm),
    strongest first. The address is a MAC on Windows/Linux and an ID on macOS."""
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    out = []
    for dev, adv in found.values():
        name = adv.local_name or dev.name or ""
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        if name.startswith("MeshCore") or BLE_UART_SERVICE in uuids:
            out.append((dev.address, name or "MeshCore radio", adv.rssi))
    return sorted(out, key=lambda r: -(r[2] if r[2] is not None else -999))


def _digest(raw: bytes) -> bytes:
    import hashlib
    return hashlib.sha256(raw).digest()[:12]


def _open_group(secret: bytes, body: bytes) -> bytes | None:
    """Decrypt a MeshCore group (channel) packet: channel hash 1, MAC 2,
    AES-128-ECB ciphertext; the MAC is HMAC-SHA256 over the ciphertext.
    None if it isn't for this channel."""
    import hashlib
    import hmac
    from Crypto.Cipher import AES
    if len(body) < 3 + 16 or (len(body) - 3) % 16:
        return None
    if body[0] != hashlib.sha256(secret).digest()[0]:
        return None
    mac, ct = body[1:3], body[3:]
    if hmac.new(secret.ljust(32, b"\0"), ct, hashlib.sha256).digest()[:2] != mac:
        return None
    return AES.new(secret, AES.MODE_ECB).decrypt(ct)
