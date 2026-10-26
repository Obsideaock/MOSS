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

    async def create_serial(self, port, baud):
        if self.port_held:
            raise PermissionError(13, "Access is denied.")
        radio = FakeRadio(self)
        self.radios.append(radio)
        self.port_held = True
        return radio

    def install(self):
        """Put this fake in place of the real meshcore package; returns an undo."""
        fake = types.ModuleType("meshcore")
        fake.EventType = EventType
        fake.MeshCore = types.SimpleNamespace(create_serial=self.create_serial)
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
        self.inbox: list[tuple[int, bytes]] = []   # (channel slot, payload)
        self.sent: list[tuple[int, bytes]] = []
        self.silent = False             # asleep: polls go unanswered
        self.closed = False
        self.commands = types.SimpleNamespace(
            get_msg=self.get_msg, send_channel_data=self.send_channel_data,
            send_device_query=self.send_device_query, get_channel=self.get_channel,
            set_channel=self.set_channel, set_flood_scope=self.set_flood_scope)

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
        if self.silent:
            return Event(EventType.ERROR, {"reason": "no_event_received"})
        if self.inbox:
            slot, raw = self.inbox.pop(0)
            await self.fire(EventType.CHANNEL_DATA_RECV,
                            {"channel_idx": slot, "data_type": 0xFF01, "payload": raw.hex()})
            return Event(EventType.CHANNEL_DATA_RECV, {})
        return Event(EventType.NO_MORE_MSGS, {})

    async def send_channel_data(self, channel, data_type, data):
        if self.silent or self.closed:
            raise OSError("write failed")
        self.sent.append((channel, data))
        return Event(EventType.OK, {})

    async def send_device_query(self):
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
