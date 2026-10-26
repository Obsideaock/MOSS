"""The #moss channel: found or created on the radio automatically.

MOSS talks only on one MeshCore *hashtag channel*, `#moss` by default. A
hashtag channel needs no secret handed around: its key is derived from the
name (first 16 bytes of SHA-256 of "#moss"), exactly as the MeshCore app does
when someone types "#moss" into "join channel". So any radio can find it, and
MOSS traffic stays off the Public channel people chat on.

On every connect MOSS looks through the radio's channel slots:

  * a slot named #moss with the matching key   -> use it
  * a slot named #moss with a *different* key  -> someone's private channel
                                                  that happens to share the
                                                  name; left alone, warned
  * otherwise                                  -> create #moss in the first
                                                  empty slot (never slot 0,
                                                  the Public channel, and
                                                  never a slot in use)
"""

from __future__ import annotations

import hashlib
import re
from typing import Callable

DEFAULT_CHANNEL = "#moss"
# What the MeshCore app allows when someone types a hashtag channel name; any
# other spelling would give a different key from the one people join in the app.
NAME_RULE = re.compile(r"^#[a-z0-9-]{1,29}$")


class ChannelError(Exception):
    pass


def check_name(name: str) -> str:
    if not NAME_RULE.match(name):
        raise ChannelError(f"{name!r} isn't a hashtag channel name: it must start with # and use "
                           f"only lowercase letters, digits and dashes (like #moss), so it matches "
                           f"what the MeshCore app creates")
    return name


def hashtag_secret(name: str) -> bytes:
    """The channel key MeshCore derives from a hashtag channel's name."""
    return hashlib.sha256(name.encode("utf-8")).digest()[:16]


async def find_or_create(mc, name: str, EventType, log: Callable[[str], None]) -> tuple[int, bool]:
    """(slot, created) for the hashtag channel `name` on this radio."""
    name = check_name(name)
    secret = hashtag_secret(name)
    max_channels = 8
    try:
        info = await mc.commands.send_device_query()
        p = getattr(info, "payload", None)
        if isinstance(p, dict) and p.get("max_channels"):
            max_channels = int(p["max_channels"])
    except Exception:
        pass                                # older firmware: assume 8 slots

    free = None
    for idx in range(max_channels):
        ev = await mc.commands.get_channel(idx)
        p = getattr(ev, "payload", None)
        if getattr(ev, "type", None) != EventType.CHANNEL_INFO or not isinstance(p, dict):
            continue
        slot_name = p.get("channel_name", "") or ""
        slot_secret = bytes(p.get("channel_secret") or b"")
        if slot_name == name and slot_secret == secret:
            return idx, False
        if slot_name == name:
            log(f"slot {idx} is a channel called {name} with a different key (a private "
                f"channel, not the public {name}); leaving it alone")
        if free is None and idx != 0 and not slot_name and not any(slot_secret):
            free = idx

    if free is None:
        raise ChannelError(f"all {max_channels} channel slots on this radio are in use. Remove one "
                           f"in the MeshCore app (or add {name} there yourself) and try again.")
    ev = await mc.commands.set_channel(free, name)
    if getattr(ev, "type", None) == EventType.ERROR:
        raise ChannelError(f"the radio refused to create {name} in slot {free}: {ev.payload}")
    check = await mc.commands.get_channel(free)
    p = getattr(check, "payload", None) or {}
    if p.get("channel_name") != name or bytes(p.get("channel_secret") or b"") != secret:
        raise ChannelError(f"created {name} in slot {free}, but reading it back didn't match")
    log(f"created the {name} channel in slot {free}")
    return free, True
