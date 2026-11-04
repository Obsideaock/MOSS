"""Pairing a Bluetooth radio with its PIN, which the Bluetooth libraries don't do.

MeshCore radios want a PIN (passkey) the first time a computer pairs with them.
The `meshcore` library never passes the PIN on, and on Windows `bleak` only
knows "just works" pairing, so on Windows MOSS pairs the radio itself through
Windows' own Bluetooth API (the `winrt` packages bleak already installs).

On a Mac, macOS shows its own PIN window, and nothing can supply the PIN for
it. On Linux, BlueZ asks through its pairing agent (`bluetoothctl`).

    await pair(address, pin)            # pair if not paired yet
    await pair(address, pin, fresh=True)  # forget any old pairing first
"""

from __future__ import annotations

import sys


class PairingError(SystemExit):
    """Pairing failed; the message says why and what to do."""


class WrongPin(PairingError):
    """The radio refused the PIN."""


def system() -> str:
    """sys.platform, behind a function so tests can pretend to be another system."""
    return sys.platform


def supported(platform: str | None = None) -> bool:
    """Can MOSS hand the PIN to the system itself here?"""
    return (platform or system()).startswith("win")


def _address_int(address: str) -> int:
    digits = address.replace(":", "").replace("-", "")
    if len(digits) != 12:
        raise PairingError(f"{address!r} isn't a Bluetooth address like 44:1B:F6:FA:00:55; "
                           f"pick the radio from the scan in setup")
    return int(digits, 16)


def _winrt():
    """The Windows Bluetooth classes (imported late: they only exist on Windows)."""
    from winrt.windows.devices.bluetooth import BluetoothLEDevice
    from winrt.windows.devices.enumeration import (DevicePairingKinds,
                                                   DevicePairingProtectionLevel,
                                                   DevicePairingResultStatus)
    return BluetoothLEDevice, DevicePairingKinds, DevicePairingProtectionLevel, DevicePairingResultStatus


def _status_name(status) -> str:
    name = getattr(status, "name", None) or str(status)
    return name.split(".")[-1].replace("_", " ").lower()


async def pair(address: str, pin: str | None, fresh: bool = False) -> str:
    """Pair with the radio using its PIN (Windows). Returns what happened, in words.

    fresh=True forgets any earlier pairing first, which is the fix when the
    radio was reflashed or reset and no longer recognises this computer.
    """
    if not supported():
        return "skipped: this system pairs through its own window"
    pin = (pin or "").strip()
    if pin and not pin.isdigit():
        raise WrongPin("the PIN should be digits only (it's in the MeshCore app's settings)")
    BluetoothLEDevice, Kinds, Level, Status = _winrt()
    device = await BluetoothLEDevice.from_bluetooth_address_async(_address_int(address))
    if device is None:
        raise PairingError(f"Windows can't find the radio {address}: is it switched on, nearby, "
                           f"and disconnected from the MeshCore phone app?")
    pairing = device.device_information.pairing
    if pairing.is_paired:
        if not fresh:
            return "already paired"
        await pairing.unpair_async()
        device = await BluetoothLEDevice.from_bluetooth_address_async(_address_int(address))
        pairing = device.device_information.pairing
    if not pairing.can_pair:
        raise PairingError(f"Windows says the radio {address} can't be paired right now; "
                           f"restart the radio and try again")

    asked_for_pin: list[bool] = []

    def on_request(sender, args):
        if args.pairing_kind == Kinds.PROVIDE_PIN:
            asked_for_pin.append(True)
            if pin:
                (getattr(args, "accept_with_pin", None) or args.accept)(pin)          # no PIN: leave it unanswered, so pairing fails cleanly
        else:
            args.accept()                 # confirm-only / numbers-match: the radio has no screen to compare

    kinds = Kinds.CONFIRM_ONLY | Kinds.PROVIDE_PIN | Kinds.CONFIRM_PIN_MATCH
    custom = pairing.custom
    token = custom.add_pairing_requested(on_request)
    try:
        result = await custom.pair_with_protection_level_async(
            kinds, Level.ENCRYPTION_AND_AUTHENTICATION)
    finally:
        custom.remove_pairing_requested(token)

    if result.status in (Status.PAIRED, Status.ALREADY_PAIRED):
        return "paired"
    reason = _status_name(result.status)
    if asked_for_pin and not pin:
        raise WrongPin("the radio wants its pairing PIN: it's in the MeshCore app's settings "
                       "for that radio")
    if result.status in (Status.AUTHENTICATION_FAILURE, Status.FAILED,
                         Status.REJECTED_BY_HANDLER) and asked_for_pin:
        raise WrongPin(f"the radio didn't accept PIN {pin} ({reason}). Check the PIN in the "
                       f"MeshCore app's settings for that radio")
    raise PairingError(f"Windows couldn't pair with the radio ({reason}). Is the MeshCore phone "
                       f"app disconnected from it? Restart the radio and try again")
