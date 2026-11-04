"""A fake MeshCore radio for tests, behaving like the real one where it matters:

- after a laptop sleep it stops answering polls, and the port refuses to
  reopen ("Access is denied") until the old handle is released;
- it has numbered channel slots kept in "flash" (they survive reconnects),
  slot 0 holding Public;
- it accepts a flood scope, if its firmware is new enough.
"""

import asyncio
import hashlib
import sys
import types

PUBLIC_KEY = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")


class EventType:
    CHANNEL_DATA_RECV = "channel_data"
    NO_MORE_MSGS = "no_more_messages"
    ERROR = "command_error"
    OK = "command_ok"
    DISCONNECTED = "disconnected"
    CHANNEL_INFO = "channel_info"
    DEVICE_INFO = "device_info"
    CHANNEL_MSG_RECV = "channel_message"
    RX_LOG_DATA = "rx_log_data"


def group_packet(secret: bytes, datagram: bytes) -> bytes:
    """A MeshCore group-data packet body as it appears in the radio's log:
    channel hash, 2-byte MAC, AES-128-ECB ciphertext of (a small header + data)."""
    import hmac
    from Crypto.Cipher import AES
    plain = b"\x01\x02\x03\x04" + b"\x01\xff" + bytes([len(datagram)]) + datagram
    plain += bytes(-len(plain) % 16)
    ct = AES.new(secret, AES.MODE_ECB).encrypt(plain)
    mac = hmac.new(secret.ljust(32, b"\0"), ct, hashlib.sha256).digest()[:2]
    return bytes([hashlib.sha256(secret).digest()[0]]) + mac + ct


class Event:
    def __init__(self, type, payload):
        self.type, self.payload = type, payload


class Factory:
    """Stands in for MeshCore.create_serial and the device around the port."""

    def __init__(self, slots=8, scopes=True):
        self.radios: list["FakeRadio"] = []
        self.port_held = False          # Windows keeps a dead handle until it's closed
        self.slots = [("", bytes(16)) for _ in range(slots)]
        self.slots[0] = ("Public", PUBLIC_KEY)
        self.scopes_supported = scopes
        self.scope_sets: list = []
        self.refuse_sends = 0           # how many sends the radio will refuse next
        self.ble_calls: list = []       # (address, pin) for each Bluetooth connect
        self.ble_missing = False        # no Bluetooth radio in range
        self.ble_error = None           # raised by a Bluetooth connect (e.g. a stale pairing)
        self.ble_gate = None            # called on each Bluetooth connect; may raise
        self.describe_late = False      # the first self-description is lost (slow Bluetooth)
        self.refuse_routes = False      # firmware that won't send along a given path

    async def create_serial(self, port, baud):
        if self.port_held:
            raise PermissionError(13, "Access is denied.")
        radio = FakeRadio(self)
        self.radios.append(radio)
        self.port_held = True
        return radio

    async def create_ble(self, address=None, pin=None, **kw):
        """Bluetooth: same radio, reached differently. Records how it was asked for."""
        self.ble_calls.append((address, pin))
        if self.ble_error is not None:
            raise self.ble_error
        if self.ble_gate is not None:
            self.ble_gate()
        if self.ble_missing:
            return None                  # nothing answered the scan
        return await self.create_serial(f"ble:{address}", 0)

    def install(self):
        """Put this fake in place of the real meshcore package; returns an undo."""
        fake = types.ModuleType("meshcore")
        fake.EventType = EventType
        fake.MeshCore = types.SimpleNamespace(create_serial=self.create_serial,
                                              create_ble=self.create_ble)
        saved = sys.modules.get("meshcore")
        sys.modules["meshcore"] = fake

        def undo():
            if saved is not None:
                sys.modules["meshcore"] = saved
            else:
                sys.modules.pop("meshcore", None)
        return undo


class FakeRadio:
    def __init__(self, factory):
        self.factory = factory
        self.subs: dict = {}
        self.inbox: list = []           # (slot, payload bytes) or (slot, "chat text")
        self.self_info = {"name": "Fake Heltec", "radio_freq": 910.525, "radio_bw": 62.5,
                          "radio_sf": 7, "radio_cr": 5, "tx_power": 22}
        self.chat_sent: list[tuple[int, str]] = []
        self.paths: list = []           # route given for each send (None = flooded)
        self.sent: list[tuple[int, bytes]] = []
        self.silent = False             # asleep: polls go unanswered
        self.unreadable = 0             # polls answered with messages the library can't parse
        self.closed = False
        self.commands = types.SimpleNamespace(
            get_msg=self.get_msg, send_channel_data=self.send_channel_data,
            send_device_query=self.send_device_query, get_channel=self.get_channel,
            set_channel=self.set_channel, set_flood_scope=self.set_flood_scope,
            send_chan_msg=self.send_chan_msg, set_radio=self.set_radio,
            send_appstart=self.send_appstart)
        if factory.describe_late:     # the radio's first self-description got lost
            self._described, self.self_info = self.self_info, {}
        self.appstarts = 0

    async def send_appstart(self, timeout=None):
        self.appstarts += 1
        if self.factory.describe_late and self.appstarts >= 2:
            self.self_info = dict(getattr(self, "_described", self.self_info))
        return Event("SELF_INFO", self.self_info)

    @property
    def is_connected(self):
        return not self.closed

    def subscribe(self, kind, cb):
        self.subs.setdefault(kind, []).append(cb)

    async def fire(self, kind, payload):
        for cb in self.subs.get(kind, []):
            await cb(Event(kind, payload))

    async def get_msg(self, timeout=None):
        await asyncio.sleep(0.005)
        if self.silent or self.unreadable:
            # silent: asleep. unreadable: the radio answers, but with a message
            # type the library can't parse, so the poll times out anyway.
            if self.unreadable:
                self.unreadable -= 1
            return Event(EventType.ERROR, {"reason": "no_event_received"})
        if self.inbox:
            slot, raw = self.inbox.pop(0)
            if isinstance(raw, str):
                await self.fire(EventType.CHANNEL_MSG_RECV,
                                {"channel_idx": slot, "text": raw, "SNR": 6.5})
                return Event(EventType.CHANNEL_MSG_RECV, {})
            await self.fire(EventType.CHANNEL_DATA_RECV,
                            {"channel_idx": slot, "data_type": 0xFF01, "payload": raw.hex()})
            return Event(EventType.CHANNEL_DATA_RECV, {})
        return Event(EventType.NO_MORE_MSGS, {})

    async def send_channel_data(self, channel, data_type, data, path=None):
        if self.silent or self.closed:
            raise OSError("write failed")
        if self.factory.refuse_sends:
            self.factory.refuse_sends -= 1
            return Event(EventType.ERROR, {"error_code": 3})     # e.g. its queue is full
        if path is not None and self.factory.refuse_routes:
            return Event(EventType.ERROR, {"error_code": 4})
        self.sent.append((channel, data))
        self.paths.append(path)
        return Event(EventType.OK, {})

    async def set_radio(self, freq, bw, sf, cr, repeat=None):
        self.self_info.update(radio_freq=freq, radio_bw=bw, radio_sf=sf, radio_cr=cr)
        return Event(EventType.OK, {})

    async def heard_flood(self, slot, raw, path: bytes, hash_size=1, route_type=1):
        """The radio hears a flooded #moss packet that came through these
        repeaters: its packet log first, then the datagram itself."""
        name, secret = self.factory.slots[slot]
        await self.fire(EventType.RX_LOG_DATA, {
            "payload_type": 6, "route_type": route_type, "path_hash_size": hash_size,
            "path": path.hex(), "pkt_payload": group_packet(secret, raw)})
        self.inbox.append((slot, raw))

    async def send_chan_msg(self, channel, text):
        if self.silent or self.closed:
            raise OSError("write failed")
        self.chat_sent.append((channel, text))
        return Event(EventType.OK, {})

    async def send_device_query(self):
        if self.silent:
            return Event(EventType.ERROR, {"reason": "no_event_received"})
        return Event(EventType.DEVICE_INFO, {"max_channels": len(self.factory.slots)})

    async def get_channel(self, idx):
        if idx >= len(self.factory.slots):
            return Event(EventType.ERROR, {"error_code": 2})
        name, secret = self.factory.slots[idx]
        return Event(EventType.CHANNEL_INFO,
                     {"channel_idx": idx, "channel_name": name, "channel_secret": secret})

    async def set_channel(self, idx, name, secret=None):
        if secret is None and name.startswith("#"):      # what the real library does
            secret = hashlib.sha256(name.encode()).digest()[:16]
        self.factory.slots[idx] = (name, secret)
        return Event(EventType.OK, {})

    async def set_flood_scope(self, scope):
        if not self.factory.scopes_supported:
            return Event(EventType.ERROR, {"reason": "unsupported"})
        self.factory.scope_sets.append(scope)
        return Event(EventType.OK, {})

    async def disconnect(self):
        self.closed = True
        self.factory.port_held = False  # releasing the handle frees the port
