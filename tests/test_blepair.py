"""Pairing a Bluetooth radio with its PIN on Windows, against a fake Windows API.

The real thing needs Windows and a radio; this checks MOSS drives the Windows
pairing calls the right way: the PIN goes in, a wrong PIN is reported as such,
an old pairing is forgotten first when asked, and setup walks through it.

    python -m unittest discover -s tests -v      (from the repo root)
"""

import asyncio
import enum
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_radio import Factory                          # noqa: E402
from moss import blepair, config, radiocheck, setup     # noqa: E402


class Kinds(enum.IntFlag):
    NONE = 0
    CONFIRM_ONLY = 1
    DISPLAY_PIN = 2
    PROVIDE_PIN = 4
    CONFIRM_PIN_MATCH = 8


class Level(enum.IntEnum):
    DEFAULT = 0
    NONE = 1
    ENCRYPTION = 2
    ENCRYPTION_AND_AUTHENTICATION = 3


class Status(enum.IntEnum):
    PAIRED = 0
    NOT_READY_TO_PAIR = 1
    NOT_PAIRED = 2
    ALREADY_PAIRED = 3
    AUTHENTICATION_FAILURE = 9
    REJECTED_BY_HANDLER = 17
    FAILED = 19


class FakeWindowsRadio:
    """What Windows knows about one radio: whether it's paired, and its real PIN."""

    def __init__(self, pin="654321", paired=False, present=True):
        self.pin, self.paired, self.present = pin, paired, present
        self.unpaired = 0
        self.pair_calls = []


class _Awaitable:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        if False:
            yield
        return self.value


def fake_winrt(radio: FakeWindowsRadio):
    """The Windows Bluetooth classes MOSS uses, faked for one radio."""

    class Args:
        def __init__(self, kind):
            self.pairing_kind = kind
            self.answer = None

        def accept(self, pin=None):
            self.answer = ("accept", pin)

        def accept_with_pin(self, pin):
            self.answer = ("accept", pin)

    class Custom:
        def __init__(self):
            self.handlers = {}

        def add_pairing_requested(self, handler):
            token = len(self.handlers) + 1
            self.handlers[token] = handler
            return token

        def remove_pairing_requested(self, token):
            del self.handlers[token]

        def pair_with_protection_level_async(self, kinds, level):
            radio.pair_calls.append((kinds, level))
            assert kinds & Kinds.PROVIDE_PIN, "must offer to provide a PIN"
            args = Args(Kinds.PROVIDE_PIN)
            for handler in list(self.handlers.values()):
                handler(self, args)
            if args.answer is None:
                status = Status.REJECTED_BY_HANDLER
            elif args.answer[1] == radio.pin:
                radio.paired = True
                status = Status.PAIRED
            else:
                status = Status.AUTHENTICATION_FAILURE
            return _Awaitable(types.SimpleNamespace(status=status))

    class Pairing:
        def __init__(self):
            self.custom = Custom()

        @property
        def is_paired(self):
            return radio.paired

        @property
        def can_pair(self):
            return True

        def unpair_async(self):
            radio.paired = False
            radio.unpaired += 1
            return _Awaitable(types.SimpleNamespace(status=0))

    class BluetoothLEDevice:
        @classmethod
        def from_bluetooth_address_async(cls, address):
            assert address == 0x441BF6FA0055, hex(address)
            if not radio.present:
                return _Awaitable(None)
            device = types.SimpleNamespace(
                device_information=types.SimpleNamespace(pairing=Pairing()))
            return _Awaitable(device)

    return lambda: (BluetoothLEDevice, Kinds, Level, Status)     # stands in for blepair._winrt


ADDR = "44:1B:F6:FA:00:55"


class WindowsPairingTests(unittest.TestCase):
    def setUp(self):
        self.radio = FakeWindowsRadio()
        for patch in (mock.patch.object(blepair, "_winrt", fake_winrt(self.radio)),
                      mock.patch.object(blepair, "system", return_value="win32")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_right_pin_pairs(self):
        self.assertEqual(asyncio.run(blepair.pair(ADDR, "654321")), "paired")
        self.assertTrue(self.radio.paired)
        kinds, level = self.radio.pair_calls[0]
        self.assertEqual(level, Level.ENCRYPTION_AND_AUTHENTICATION)

    def test_a_wrong_pin_says_so(self):
        with self.assertRaisesRegex(blepair.WrongPin, "didn't accept PIN 123456"):
            asyncio.run(blepair.pair(ADDR, "123456"))
        self.assertFalse(self.radio.paired)

    def test_no_pin_when_the_radio_wants_one(self):
        with self.assertRaisesRegex(blepair.WrongPin, "wants its pairing PIN"):
            asyncio.run(blepair.pair(ADDR, None))

    def test_letters_are_not_a_pin(self):
        with self.assertRaisesRegex(blepair.WrongPin, "digits only"):
            asyncio.run(blepair.pair(ADDR, "12ab56"))

    def test_already_paired_is_left_alone(self):
        self.radio.paired = True
        self.assertEqual(asyncio.run(blepair.pair(ADDR, "654321")), "already paired")
        self.assertEqual((self.radio.unpaired, self.radio.pair_calls), (0, []))

    def test_fresh_forgets_an_old_pairing_first(self):
        self.radio.paired = True                    # stale: the radio forgot this computer
        self.assertEqual(asyncio.run(blepair.pair(ADDR, "654321", fresh=True)), "paired")
        self.assertEqual(self.radio.unpaired, 1)
        self.assertTrue(self.radio.paired)

    def test_radio_not_found(self):
        self.radio.present = False
        with self.assertRaisesRegex(blepair.PairingError, "can't find the radio"):
            asyncio.run(blepair.pair(ADDR, "654321"))

    def test_a_name_is_not_an_address(self):
        with self.assertRaisesRegex(blepair.PairingError, "isn't a Bluetooth address"):
            asyncio.run(blepair.pair("MeshCore-IVIORT", "654321"))

    def test_other_systems_do_nothing(self):
        with mock.patch.object(blepair, "system", return_value="darwin"):
            self.assertIn("skipped", asyncio.run(blepair.pair(ADDR, "654321")))
        self.assertEqual(self.radio.pair_calls, [])


class WindowsSetupTests(unittest.TestCase):
    """Setup on Windows: the first connect fails unpaired; MOSS pairs with the PIN."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.radio = FakeWindowsRadio(pin="654321")
        self.factory = Factory()
        self.addCleanup(self.factory.install())
        for patch in (mock.patch.dict(os.environ, {"MOSS_HOME": self.home}),
                      mock.patch.object(blepair, "_winrt", fake_winrt(self.radio)),
                      mock.patch.object(blepair, "system", return_value="win32"),
                      mock.patch("moss.transport.asyncio.sleep", new=_no_sleep),
                      mock.patch.object(setup, "PAIR_SETTLE_WAITS", (0, 0, 0))):
            patch.start()
            self.addCleanup(patch.stop)

        def gate():
            if not self.radio.paired:                 # Windows won't talk to it unpaired
                raise ConnectionError("Failed to connect to device")
        self.factory.ble_gate = gate

    def _run_setup(self, answers):
        from rich.console import Console
        from moss import ui
        pins = iter(answers)
        with mock.patch.object(ui, "console", Console(file=io.StringIO())), \
                mock.patch.object(ui, "confirm", side_effect=lambda q, d=True: any(
                    w in q.lower() for w in ("pair", "pin"))), \
                mock.patch.object(ui, "text", side_effect=lambda *a, **k: next(pins)), \
                mock.patch.object(setup, "_pick_link", return_value=radiocheck.Link(ble=ADDR)):
            return setup._setup_reader(ui, config.load(), None, True)

    def test_pairs_with_the_pin_then_saves_the_radio(self):
        self.assertEqual(self._run_setup(["654321"]), 0)
        self.assertTrue(self.radio.paired)
        cfg = config.load()
        self.assertEqual(cfg["ble"], ADDR)
        self.assertNotIn("pin", cfg)                  # some radios change PIN every pairing

    def test_a_wrong_pin_asks_again(self):
        self.assertEqual(self._run_setup(["123456", "654321"]), 0)
        self.assertEqual(len(self.radio.pair_calls), 2)
        self.assertTrue(self.radio.paired)

    def test_waits_for_windows_to_settle_after_pairing(self):
        """Real Windows: the first connection right after pairing fails, a later one works."""
        tries = []

        def gate():
            tries.append(1)
            if not self.radio.paired or len(tries) < 3:
                raise ConnectionError("Failed to connect to device")
        self.factory.ble_gate = gate
        self.assertEqual(self._run_setup(["654321"]), 0)
        self.assertGreaterEqual(len(tries), 3)

    def test_saved_pin_pairs_before_connecting(self):
        """Later runs: the transport pairs first when it has a PIN (no-op once paired)."""
        from moss.transport import MeshCoreTransport

        async def go():
            tx = await MeshCoreTransport.open(None, 115200, ble=ADDR, pin="654321", log=lambda m: None)
            await tx.close()
        asyncio.run(asyncio.wait_for(go(), 30))
        self.assertTrue(self.radio.paired)


async def _no_sleep(*_a, **_k):
    return None


if __name__ == "__main__":
    unittest.main()
