"""The radio link survives the laptop sleeping and the cable being pulled.

Uses a fake MeshCore radio that behaves like the real one does on Windows:
after sleep it stops answering polls, and the port refuses to reopen
("Access is denied") until the old handle is released.

    python -m unittest discover -s tests -v      (from the repo root)
"""

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from moss.transport import MeshCoreTransport   # noqa: E402


from fake_radio import EventType, Factory  # noqa: E402


class ReconnectTests(unittest.TestCase):
    def setUp(self):
        self.factory = Factory()
        self.addCleanup(self.factory.install())

    def run_async(self, coro):
        return asyncio.run(asyncio.wait_for(coro, 30))

    async def open(self):
        tx = await MeshCoreTransport.open("COM12", 115200, "#moss", log=self.log.append)
        tx._poll_interval = 0.01
        tx.RETRY_DELAY = 0.05
        tx.POLL_TIMEOUT = 0.05
        heard: list[bytes] = []
        tx.subscribe(heard.append)
        return tx, heard

    async def wait_for(self, cond, secs=5.0):
        for _ in range(int(secs / 0.02)):
            if cond():
                return True
            await asyncio.sleep(0.02)
        return False

    def test_wakes_up_after_sleep(self):
        self.log = []

        async def go():
            tx, heard = await self.open()
            first = self.factory.radios[0]
            first.inbox.append((tx.channel, b"\\x02before"))
            self.assertTrue(await self.wait_for(lambda: heard))
            # The laptop sleeps: the radio stops answering, the port stays held.
            first.silent = True
            self.assertTrue(await self.wait_for(lambda: tx.state == "reconnecting"))
            # A send while it's down waits for the link instead of failing.
            sending = asyncio.ensure_future(tx.send(b"\\x05request"))
            self.assertTrue(await self.wait_for(lambda: tx.state == "connected" and tx.reconnects == 1))
            await sending
            second = self.factory.radios[-1]
            self.assertIsNot(first, second)
            self.assertTrue(first.closed, "the old handle must be released or Windows refuses the port")
            self.assertEqual(second.sent, [(tx.channel, b"\\x05request")])
            second.inbox.append((tx.channel, b"\\x02after"))
            self.assertTrue(await self.wait_for(lambda: len(heard) == 2))
            await tx.close()
            return heard

        heard = self.run_async(go())
        self.assertEqual(heard, [b"\\x02before", b"\\x02after"])
        self.assertTrue(any("radio link lost" in m and "Reconnecting" in m for m in self.log), self.log)
        self.assertTrue(any("radio back" in m for m in self.log))

    def test_cable_pulled(self):
        self.log = []

        async def go():
            tx, heard = await self.open()
            first = self.factory.radios[0]
            await first.fire(EventType.DISCONNECTED, {"reason": "serial_disconnect"})
            self.assertTrue(await self.wait_for(lambda: tx.reconnects == 1 and tx.state == "connected"))
            self.factory.radios[-1].inbox.append((tx.channel, b"\\x02hello"))
            self.assertTrue(await self.wait_for(lambda: heard == [b"\\x02hello"]))
            await tx.close()
            self.assertEqual(tx.state, "closed")

        self.run_async(go())

    def test_failed_write_triggers_reconnect(self):
        self.log = []

        async def go():
            tx, _ = await self.open()
            self.factory.radios[0].closed = True     # gone, but no event was sent
            await tx.send(b"\\x05x")                  # dropped like a lost datagram, no exception
            self.assertTrue(await self.wait_for(lambda: tx.reconnects == 1 and tx.state == "connected"))
            await tx.close()

        self.run_async(go())


class FalseAlarmTests(unittest.TestCase):
    """2026-10-29: a healthy radio was declared lost. Polls can go unanswered
    when the radio sends message types the library can't parse; that alone
    must not tear the link down."""

    def setUp(self):
        self.factory = Factory()
        self.addCleanup(self.factory.install())
        self.log = []

    def test_unreadable_messages_dont_disconnect(self):
        async def go():
            tx = await MeshCoreTransport.open("COM12", 115200, "#moss", log=self.log.append)
            tx._poll_interval, tx.RETRY_DELAY, tx.POLL_TIMEOUT = 0.01, 0.05, 0.05
            heard = []
            tx.subscribe(heard.append)
            radio = self.factory.radios[0]
            radio.unreadable = 10                 # ten odd messages in a row
            for _ in range(100):
                if not radio.unreadable:
                    break
                await asyncio.sleep(0.02)
            radio.inbox.append((tx.channel, b"\x02still here"))
            for _ in range(100):
                if heard:
                    break
                await asyncio.sleep(0.02)
            await tx.close()
            return tx, heard

        tx, heard = asyncio.run(asyncio.wait_for(go(), 30))
        self.assertEqual(tx.reconnects, 0, self.log)
        self.assertEqual(len(self.factory.radios), 1, "it reopened a working radio")
        self.assertEqual(heard, [b"\x02still here"])

    def test_lost_link_says_why(self):
        async def go():
            tx = await MeshCoreTransport.open("COM12", 115200, "#moss", log=self.log.append)
            tx._poll_interval, tx.RETRY_DELAY, tx.POLL_TIMEOUT = 0.01, 0.05, 0.05
            self.factory.radios[0].silent = True
            for _ in range(250):
                if tx.reconnects:
                    break
                await asyncio.sleep(0.02)
            await tx.close()

        asyncio.run(asyncio.wait_for(go(), 30))
        self.assertTrue(any("stopped answering" in m for m in self.log), self.log)

    def test_failed_reconnects_are_reported(self):
        async def go():
            tx = await MeshCoreTransport.open("COM12", 115200, "#moss", log=self.log.append)
            tx._poll_interval, tx.RETRY_DELAY, tx.POLL_TIMEOUT = 0.01, 0.05, 0.05
            first = self.factory.radios[0]
            real_disconnect = first.disconnect

            async def stuck_disconnect():      # Windows keeps the port held
                first.closed = True
            first.disconnect = stuck_disconnect
            first.silent = True
            for _ in range(100):
                if any("can't reopen" in m for m in self.log):
                    break
                await asyncio.sleep(0.02)
            await real_disconnect()            # finally released
            for _ in range(250):
                if tx.reconnects:
                    break
                await asyncio.sleep(0.02)
            await tx.close()
            return tx

        tx = asyncio.run(asyncio.wait_for(go(), 30))
        self.assertTrue(any("can't reopen COM12" in m and "Access is denied" in m for m in self.log), self.log)
        self.assertEqual(tx.reconnects, 1)


class RefusedSendTests(unittest.TestCase):
    """The radio can refuse a send (queue full, busy) by replying with an
    error. That used to be ignored silently."""

    def setUp(self):
        self.factory = Factory()
        self.addCleanup(self.factory.install())
        self.log = []

    async def open(self):
        tx = await MeshCoreTransport.open("COM12", 115200, "#moss", log=self.log.append)
        tx._poll_interval, tx.RETRY_DELAY, tx.POLL_TIMEOUT = 0.01, 0.05, 0.05
        return tx

    def test_a_refusal_is_retried(self):
        async def go():
            tx = await self.open()
            self.factory.refuse_sends = 2
            await tx.send(b"\x05ask")
            await tx.close()
            return tx

        tx = asyncio.run(go())
        self.assertEqual(self.factory.radios[0].sent, [(tx.channel, b"\x05ask")])
        self.assertEqual(tx.refused, 0)

    def test_persistent_refusal_is_logged_then_the_link_reopened(self):
        async def go():
            tx = await self.open()
            self.factory.refuse_sends = 10 ** 6
            for _ in range(tx.STUCK_AFTER):
                await tx.send(b"\x05ask")
            self.factory.refuse_sends = 0
            for _ in range(250):
                if tx.reconnects:
                    break
                await asyncio.sleep(0.02)
            await tx.close()
            return tx

        tx = asyncio.run(asyncio.wait_for(go(), 60))
        self.assertEqual(tx.refused, tx.STUCK_AFTER)
        self.assertTrue(any("refused to send" in m for m in self.log))
        self.assertTrue(any("refused 6 messages in a row" in m for m in self.log), self.log)
        self.assertEqual(tx.reconnects, 1)


if __name__ == "__main__":
    unittest.main()
