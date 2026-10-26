"""The #moss channel: found or created on the radio, and the only one used.

    python -m unittest discover -s tests -v      (from the repo root)
"""

import asyncio
import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_radio import PUBLIC_KEY, Factory          # noqa: E402
from moss.channel import ChannelError, check_name, hashtag_secret   # noqa: E402
from moss.transport import MeshCoreTransport        # noqa: E402

MOSS_KEY = hashlib.sha256(b"#moss").digest()[:16]


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


class NameTests(unittest.TestCase):
    def test_key_is_what_the_meshcore_app_derives(self):
        # sha256("#moss")[:16], the rule MeshCore uses for hashtag channels.
        self.assertEqual(hashtag_secret("#moss"), MOSS_KEY)
        self.assertEqual(len(MOSS_KEY), 16)

    def test_only_app_style_names(self):
        self.assertEqual(check_name("#moss"), "#moss")
        self.assertEqual(check_name("#moss-test2"), "#moss-test2")
        for bad in ("moss", "#Moss", "#moss channel", "#" + "a" * 30, "#", "Public"):
            with self.assertRaises(ChannelError, msg=bad):
                check_name(bad)


class SlotTests(unittest.TestCase):
    def setUp(self):
        self.log = []

    def open(self, factory, **kw):
        undo = factory.install()
        self.addCleanup(undo)
        return MeshCoreTransport.open("COM12", 115200, log=self.log.append, **kw)

    def test_creates_moss_in_first_free_slot(self):
        f = Factory()
        f.slots[1] = ("my friends", bytes(range(16)))       # in use: never touched

        async def go():
            tx = await self.open(f)
            await tx.close()
            return tx.channel

        slot = run(go())
        self.assertEqual(slot, 2)
        self.assertEqual(f.slots[2], ("#moss", MOSS_KEY))
        self.assertEqual(f.slots[0], ("Public", PUBLIC_KEY))
        self.assertEqual(f.slots[1], ("my friends", bytes(range(16))))
        self.assertTrue(any("created the #moss channel in slot 2" in m for m in self.log))

    def test_uses_existing_moss_without_rewriting(self):
        f = Factory()
        f.slots[5] = ("#moss", MOSS_KEY)                   # added earlier in the app

        async def go():
            tx = await self.open(f)
            await tx.close()
            return tx.channel

        self.assertEqual(run(go()), 5)
        self.assertEqual(sum(1 for n, _ in f.slots if n == "#moss"), 1, "made a second #moss")
        self.assertFalse(any("created" in m for m in self.log))

    def test_private_channel_named_moss_is_left_alone(self):
        f = Factory()
        f.slots[1] = ("#moss", b"\x99" * 16)               # same name, someone's own key

        async def go():
            tx = await self.open(f)
            await tx.close()
            return tx.channel

        slot = run(go())
        self.assertNotEqual(slot, 1)
        self.assertEqual(f.slots[1], ("#moss", b"\x99" * 16))
        self.assertEqual(f.slots[slot], ("#moss", MOSS_KEY))
        self.assertTrue(any("different key" in m for m in self.log))

    def test_never_overwrites_public_or_used_slots(self):
        f = Factory(slots=3)
        f.slots[1] = ("a", b"\x01" * 16)
        f.slots[2] = ("b", b"\x02" * 16)

        with self.assertRaisesRegex(SystemExit, "all 3 channel slots"):
            run(self.open(f))
        self.assertEqual(f.slots[0], ("Public", PUBLIC_KEY))
        self.assertEqual([n for n, _ in f.slots], ["Public", "a", "b"])

    def test_only_moss_traffic_is_heard_and_sent(self):
        f = Factory()

        async def go():
            tx = await self.open(f)
            heard = []
            tx.subscribe(heard.append)
            radio = f.radios[-1]
            radio.inbox += [(0, b"\x02public chatter"), (tx.channel, b"\x02for moss"),
                            (7, b"\x02another channel")]
            for _ in range(100):
                if not radio.inbox:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.05)
            await tx.send(b"\x05hello")
            await tx.close()
            return tx.channel, heard, radio.sent

        slot, heard, sent = run(go())
        self.assertEqual(heard, [b"\x02for moss"])
        self.assertEqual(sent, [(slot, b"\x05hello")])

    def test_scope_set_and_kept_after_reconnect(self):
        f = Factory()

        async def go():
            tx = await self.open(f, scope="#colorado")
            tx.RETRY_DELAY, tx.POLL_TIMEOUT, tx._poll_interval = 0.05, 0.05, 0.01
            first = f.radios[-1]
            first.silent = True                             # laptop sleeps
            for _ in range(250):
                if tx.reconnects:
                    break
                await asyncio.sleep(0.02)
            await tx.close()
            return tx

        tx = run(go())
        self.assertTrue(tx.scoped)
        self.assertEqual(tx.reconnects, 1)
        self.assertEqual(f.scope_sets, ["#colorado", "#colorado"], "scope must be set again after reconnecting")

    def test_old_firmware_without_scopes_still_works(self):
        f = Factory(scopes=False)

        async def go():
            tx = await self.open(f, scope="#colorado")
            await tx.close()
            return tx

        tx = run(go())
        self.assertFalse(tx.scoped)
        self.assertTrue(any("sending unscoped" in m for m in self.log))

    def test_bad_name_refused_before_touching_the_radio(self):
        f = Factory()
        with self.assertRaises(ChannelError):
            run(self.open(f, channel_name="Public"))
        self.assertEqual(f.radios, [])


if __name__ == "__main__":
    unittest.main()
