"""Bluetooth radios (a T1000-E, say): the same MOSS, reached without a cable.

These use the fake radio; with real hardware, `moss setup` → Bluetooth, then
`moss radio-check info` and `moss selftest`.

    python -m unittest discover -s tests -v      (from the repo root)
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_radio import Factory                          # noqa: E402
from moss import config, radiocheck, setup              # noqa: E402
from moss.transport import MeshCoreTransport            # noqa: E402


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


class BleTransportTests(unittest.TestCase):
    def setUp(self):
        self.factory = Factory()
        self.addCleanup(self.factory.install())
        self.log = []

    def test_connects_and_finds_moss(self):
        async def go():
            tx = await MeshCoreTransport.open(None, 115200, ble="AA:BB:CC:DD:EE:FF", pin="123456",
                                              log=self.log.append)
            heard = []
            tx.subscribe(heard.append)
            radio = self.factory.radios[-1]
            radio.inbox.append((tx.channel, b"\x02over the air"))
            for _ in range(100):
                if heard:
                    break
                await asyncio.sleep(0.02)
            await tx.send(b"\x05ask")
            await tx.close()
            return tx, heard, radio
        tx, heard, radio = run(go())
        # the PIN never reaches meshcore: there it only triggers a pairing macOS can't do
        self.assertEqual(self.factory.ble_calls, [("AA:BB:CC:DD:EE:FF", None)])
        self.assertEqual(heard, [b"\x02over the air"])
        self.assertEqual(radio.sent, [(tx.channel, b"\x05ask")])
        self.assertIn("Bluetooth", tx.where)
        self.assertTrue(any(n == "#moss" for n, _ in self.factory.slots))

    def test_asks_again_when_the_radio_description_is_late(self):
        """Bluetooth radios (a T-Beam, say) can answer the first 'who are you?' too late."""
        self.factory.describe_late = True

        async def go():
            tx = await MeshCoreTransport.open(None, 115200, ble="AA:BB", log=self.log.append)
            info = tx.radio_info()
            await tx.close()
            return info, self.factory.radios[-1].appstarts
        info, asks = run(go())
        self.assertEqual(info.get("radio_freq"), 910.525)
        self.assertEqual(asks, 2)

    def test_any_radio_means_no_address(self):
        async def go():
            tx = await MeshCoreTransport.open(None, 115200, ble="", log=self.log.append)
            await tx.close()
        run(go())
        self.assertEqual(self.factory.ble_calls[0], (None, None))

    def test_not_found_gives_a_helpful_message(self):
        self.factory.ble_missing = True
        with mock.patch("moss.transport.asyncio.sleep", new=_no_sleep):
            with self.assertRaisesRegex(SystemExit, "phone app"):
                run(MeshCoreTransport.open(None, 115200, ble="MeshCore-Stern", log=self.log.append))
        self.assertEqual(len(self.factory.ble_calls), 3, "should retry a few times")

    def test_stale_pairing_fails_at_once_with_the_fix(self):
        from moss.transport import StalePairing
        self.factory.ble_error = Exception(
            'failed to connect: Error Domain=CBErrorDomain Code=14 '
            '"Peer removed pairing information"')
        with mock.patch("moss.blepair.system", return_value="darwin"):
            with self.assertRaises(StalePairing) as caught:
                run(MeshCoreTransport.open(None, 115200, ble="A2A0849B", log=self.log.append))
        self.assertEqual(len(self.factory.ble_calls), 1, "retrying can't fix a stale pairing")
        text = str(caught.exception)
        self.assertIn("Forget This Device", text)
        self.assertIn("old pairing", text)
        self.assertIsInstance(caught.exception, SystemExit)

    def test_stale_pairing_help_per_system(self):
        from moss.transport import is_stale_pairing, stale_pairing_help
        self.assertIn("Remove device", stale_pairing_help("AA", platform="win32"))
        self.assertIn("bluetoothctl remove AA:BB", stale_pairing_help("AA:BB", platform="linux"))
        self.assertTrue(is_stale_pairing(Exception("org.bluez.Error.AuthenticationFailed")))
        self.assertFalse(is_stale_pairing(Exception("device not found")))

    def test_other_errors_still_retry_with_mac_pin_note(self):
        self.factory.ble_error = Exception("device went away")
        with mock.patch("moss.transport.asyncio.sleep", new=_no_sleep), \
                mock.patch("moss.blepair.system", return_value="darwin"):
            with self.assertRaisesRegex(SystemExit, "your Mac shows its own window"):
                run(MeshCoreTransport.open(None, 115200, ble="A2A0849B", log=self.log.append))
        self.assertEqual(len(self.factory.ble_calls), 3)

    def test_reconnects_over_bluetooth_not_serial(self):
        async def go():
            tx = await MeshCoreTransport.open(None, 115200, ble="AA:BB", log=self.log.append)
            tx.RETRY_DELAY, tx.POLL_TIMEOUT, tx._poll_interval = 0.05, 0.05, 0.01
            self.factory.radios[-1].silent = True           # walked out of range
            for _ in range(250):
                if tx.reconnects:
                    break
                await asyncio.sleep(0.02)
            await tx.close()
            return tx
        tx = run(go())
        self.assertEqual(tx.reconnects, 1)
        self.assertEqual([a for a, _ in self.factory.ble_calls], ["AA:BB", "AA:BB"])
        self.assertTrue(any("radio back on Bluetooth" in m for m in self.log), self.log)


async def _no_sleep(*_a, **_k):
    return None


class BleSettingsTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"MOSS_HOME": self.home})
        p.start()
        self.addCleanup(p.stop)
        self.factory = Factory()
        self.addCleanup(self.factory.install())

    def test_saved_bluetooth_radio_is_used_and_port_overrides_it(self):
        from moss.cli import build_parser
        config.update(ble="AA:BB", port=None)
        args = build_parser().parse_args(["search", "x"])
        self.assertEqual((args.ble, args.pin, args.port), ("AA:BB", None, None))
        args = build_parser().parse_args(["search", "x", "--port", "COM5"])
        self.assertEqual((args.ble, args.port), (None, "COM5"))
        config.update(ble=None, port="COM12")
        args = build_parser().parse_args(["search", "x", "--ble"])
        self.assertEqual(args.ble, "")

    def test_setup_saves_a_bluetooth_reader(self):
        import io
        from rich.console import Console
        from moss import ui
        with mock.patch.object(ui, "console", Console(file=io.StringIO())):
            code = setup.run(role="reader", ble="AA:BB", pin="123456", yes=True)
        self.assertEqual(code, 0)
        cfg = config.load()
        self.assertEqual((cfg["role"], cfg["ble"], cfg["port"]), ("reader", "AA:BB", None))
        self.assertNotIn("pin", cfg)
        self.assertNotIn("pin", json.loads(config.path().read_text()))
        self.assertEqual(config.radio_label(cfg), "Bluetooth (AA:BB)")

    def test_an_old_saved_pin_is_ignored_and_dropped(self):
        config.path().parent.mkdir(parents=True, exist_ok=True)
        config.path().write_text(json.dumps({"role": "reader", "ble": "AA:BB", "pin": "123456"}))
        self.assertNotIn("pin", config.load())
        config.update(scope=None)
        self.assertNotIn("pin", json.loads(config.path().read_text()))
        self.assertIsNone(radiocheck.Link.of(None).pin)

    def test_setup_offers_a_retry_after_forgetting_a_stale_pairing(self):
        import io
        from rich.console import Console
        from moss import ui
        self.factory.ble_error = Exception('Error Domain=CBErrorDomain Code=14 '
                                           '"Peer removed pairing information"')
        asked = []

        def confirm(question, default=True):
            asked.append(question)
            if "Forgotten the radio" in question:
                self.factory.ble_error = None          # the user forgot it in Settings
                return True
            return False
        link = radiocheck.Link(ble="A2A0849B")
        with mock.patch.object(ui, "console", Console(file=io.StringIO())), \
                mock.patch.object(ui, "confirm", side_effect=confirm), \
                mock.patch.object(ui, "text", side_effect=AssertionError("no PIN prompt")), \
                mock.patch.object(setup, "_pick_link", return_value=link):
            code = setup._setup_reader(ui, config.load(), None, True)
        self.assertEqual(code, 0)
        self.assertTrue(any("Forgotten the radio" in q for q in asked), asked)
        self.assertEqual(config.load()["ble"], "A2A0849B")

    def test_radio_check_uses_the_saved_bluetooth_radio(self):
        config.update(ble="AA:BB", port=None)
        out = []
        asyncio.run(radiocheck.send(None, count=2, gap=0, out=out.append))
        self.assertEqual(self.factory.ble_calls, [("AA:BB", None)])
        self.assertTrue(any("connected over Bluetooth" in m for m in out), out)


if __name__ == "__main__":
    unittest.main()
