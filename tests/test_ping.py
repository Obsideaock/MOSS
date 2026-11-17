"""Range testing: the phone app sends PINGs, the Seed Vault answers each with
a PONG saying how well it heard it.

Over the simulated radio and the fake MeshCore radio; no hardware. On real
radios: the MossRadio iPhone app's Range tab, with a Seed Vault running.

    python -m unittest discover -s tests -v      (from the repo root)
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_radio import Factory                              # noqa: E402
from moss import protocol as P                              # noqa: E402
from moss import seal as S                                  # noqa: E402
from moss.gateway import Gateway                            # noqa: E402
from moss.sources import LocalSource                        # noqa: E402
from moss.transport import MeshCoreTransport, SimNetwork    # noqa: E402

FIXTURES = ROOT / "TestExamples"


def run(coro, limit=60):
    return asyncio.run(asyncio.wait_for(coro, limit))


async def until(check, seconds=3.0):
    for _ in range(int(seconds / 0.02)):
        if check():
            return True
        await asyncio.sleep(0.02)
    return check()


class PingFormatTests(unittest.TestCase):
    def test_ping_and_pong_round_trip(self):
        self.assertEqual(P.parse_ping(P.build_ping(0xBEEF, 7, P.PING_ZERO_HOP)),
                         (0xBEEF, 7, P.PING_ZERO_HOP))
        pong = P.parse_pong(P.build_pong(0xBEEF, 7, -3.25, -101, 255, 1, "ATLAS vault"))
        self.assertEqual(pong, {"pid": 0xBEEF, "seq": 7, "snr": -3.25, "rssi": -101,
                                "hops": 255, "flags": 1, "name": "ATLAS vault"})

    def test_unknown_signal_stays_unknown(self):
        pong = P.parse_pong(P.build_pong(1, 2, None, None, None, 0))
        self.assertIsNone(pong["snr"])
        self.assertIsNone(pong["rssi"])
        self.assertEqual(pong["hops"], 255)

    def test_extreme_values_are_clamped_not_wrapped(self):
        pong = P.parse_pong(P.build_pong(1, 2, 99.0, -300, 3, 0))
        self.assertEqual(pong["snr"], 126 / 4)
        self.assertEqual(pong["rssi"], -128)

    def test_pongs_always_fit_one_datagram(self):
        self.assertLessEqual(len(P.build_pong(1, 2, 1, 1, 1, 0, "é" * 200)), P.MAX_DATAGRAM)


class SimPingTests(unittest.TestCase):
    """The vault and a phone on the simulated radio."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.net = SimNetwork()
        self.keys = S.VaultKeys.load_or_create(self.home / "keys", name="ATLAS")

    def world(self, **options):
        gw = Gateway(self.net.endpoint("vault"), LocalSource(FIXTURES, None), gap=0.0,
                     keys=self.keys, log=lambda m: None, **options)
        phone = self.net.endpoint("phone")
        heard: list[bytes] = []
        phone.subscribe(heard.append)
        return gw, phone, heard

    def pongs(self, heard):
        return [P.parse_pong(raw) for raw in heard if raw[0] == P.PONG]

    def test_vault_answers_with_how_it_heard_the_ping(self):
        async def go():
            gw, phone, heard = self.world()
            await gw.start()
            heard.clear()                                   # the start-up HELLO
            await phone.send(P.build_ping(0x1234, 1))
            await until(lambda: self.pongs(heard))
            await gw.stop()
            return self.pongs(heard), phone.last_signal
        pongs, how_phone_heard = run(go())
        self.assertEqual(len(pongs), 1)
        pong = pongs[0]
        self.assertEqual((pong["pid"], pong["seq"]), (0x1234, 1))
        self.assertEqual((pong["snr"], pong["rssi"], pong["hops"]), (8.0, -70, 0))
        self.assertEqual(pong["name"], "ATLAS")
        self.assertEqual(pong["flags"], 0, "a flooded ping gets a flooded answer")
        self.assertEqual(how_phone_heard["hops"], 0)

    def test_zero_hop_ping_gets_a_zero_hop_answer(self):
        async def go():
            gw, phone, heard = self.world()
            await gw.start()
            heard.clear()
            await phone.send(P.build_ping(9, 1, P.PING_ZERO_HOP), zero_hop=True)
            await until(lambda: self.pongs(heard))
            await gw.stop()
            return self.pongs(heard), phone.last_signal
        pongs, how_phone_heard = run(go())
        self.assertEqual(pongs[0]["hops"], 255, "the vault saw it arrive by direct route")
        self.assertEqual(pongs[0]["flags"], P.PING_ZERO_HOP)
        self.assertEqual(how_phone_heard["hops"], 255, "and answered without repeaters")

    def test_rate_limited_and_each_ping_answered_once(self):
        async def go():
            gw, phone, heard = self.world(ping_gap=0.5)
            await gw.start()
            heard.clear()
            ping = P.build_ping(5, 1)
            await phone.send(ping)
            await phone.send(ping)                         # a repeated copy
            await phone.send(P.build_ping(5, 2))           # too soon after the last answer
            await asyncio.sleep(0.6)
            await phone.send(P.build_ping(5, 3))           # fine again
            await until(lambda: len(self.pongs(heard)) >= 2)
            await asyncio.sleep(0.1)
            await gw.stop()
            return [p["seq"] for p in self.pongs(heard)], gw.pongs
        seqs, count = run(go())
        self.assertEqual(seqs, [1, 3])
        self.assertEqual(count, 2)

    def test_can_be_turned_off(self):
        async def go():
            gw, phone, heard = self.world(answer_pings=False)
            await gw.start()
            heard.clear()
            await phone.send(P.build_ping(5, 1))
            await asyncio.sleep(0.3)
            await gw.stop()
            return self.pongs(heard)
        self.assertEqual(run(go()), [])

    def test_a_short_ping_is_ignored(self):
        async def go():
            gw, phone, heard = self.world()
            await gw.start()
            heard.clear()
            await phone.send(bytes([P.PING, 1, 2]))
            await asyncio.sleep(0.3)
            await gw.stop()
            return self.pongs(heard), gw.sessions
        pongs, sessions = run(go())
        self.assertEqual(pongs, [])


class RadioPingTests(unittest.TestCase):
    """The vault on a (fake) MeshCore radio: real SNR/RSSI/hops go into the answer."""

    def setUp(self):
        self.factory = Factory()
        self.addCleanup(self.factory.install())
        self.home = Path(tempfile.mkdtemp())

    def test_answer_carries_the_radio_signal_and_goes_zero_hop(self):
        async def go():
            tx = await MeshCoreTransport.open("COM12", 115200, log=lambda m: None)
            keys = S.VaultKeys.load_or_create(self.home / "keys", name="ATLAS")
            gw = Gateway(tx, LocalSource(FIXTURES, None), gap=0.0, keys=keys, log=lambda m: None)
            await gw.start()
            radio = self.factory.radios[-1]
            sent_before = len(radio.sent)
            radio.inbox.append((tx.channel, P.build_ping(0x77, 4, P.PING_ZERO_HOP),
                                {"SNR": 7.25, "path_len": 255, "rssi": -88}))
            await until(lambda: any(d[0] == P.PONG for _, d in radio.sent[sent_before:]), 5)
            await gw.stop()
            await tx.close()
            return radio
        radio = run(go())
        sent = [(d, path) for (_, d), path in zip(radio.sent, radio.paths) if d[0] == P.PONG]
        self.assertEqual(len(sent), 1)
        pong, path = sent[0]
        p = P.parse_pong(pong)
        self.assertEqual((p["pid"], p["seq"], p["snr"], p["rssi"], p["hops"]),
                         (0x77, 4, 7.25, -88, 255))
        self.assertEqual(path, b"", "zero-hop: a direct route with no repeaters")

    def test_flooded_ping_without_rssi(self):
        async def go():
            tx = await MeshCoreTransport.open("COM12", 115200, log=lambda m: None)
            gw = Gateway(tx, LocalSource(FIXTURES, None), gap=0.0, log=lambda m: None)
            await gw.start()
            radio = self.factory.radios[-1]
            radio.inbox.append((tx.channel, P.build_ping(1, 1), {"SNR": -2.5, "path_len": 2}))
            await until(lambda: any(d[0] == P.PONG for _, d in radio.sent), 5)
            await gw.stop()
            await tx.close()
            return radio
        radio = run(go())
        pongs = [(d, path) for (_, d), path in zip(radio.sent, radio.paths) if d[0] == P.PONG]
        p = P.parse_pong(pongs[0][0])
        self.assertEqual((p["snr"], p["rssi"], p["hops"], p["flags"]), (-2.5, None, 2, 0))
        self.assertIsNone(pongs[0][1], "flooded, like the ping")


if __name__ == "__main__":
    unittest.main()
