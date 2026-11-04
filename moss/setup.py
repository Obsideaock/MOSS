"""`moss setup`: make this machine a reader or a Seed Vault.

Run by the installers at the end, and safe to run again any time (from
the menu, or `moss setup`). Everything it finds out is remembered in
~/.moss/config.json; on a Seed Vault it also writes the service settings
(/etc/moss/gateway.env) and installs the service. Existing vault keys,
settings and identities are always kept.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from . import config, radiocheck, service

ROLES = ("reader", "vault")


# ---------------------------------------------------------------- finding things

def find_ports() -> list[tuple[str, str]]:
    """(port, description), radios first. On Linux, the stable
    /dev/serial/by-id name is used in place of /dev/ttyUSB0-style names."""
    try:
        from serial.tools import list_ports
        ports = [p for p in list_ports.comports() if "bluetooth" not in (p.description or "").lower()]
    except ImportError:
        ports = []
    by_id = {}
    folder = Path("/dev/serial/by-id")
    if folder.is_dir():
        for link in folder.iterdir():
            by_id[str(link.resolve())] = str(link)
    out = []
    for p in ports:
        desc = p.description or ""
        if p.manufacturer and p.manufacturer not in desc:
            desc = f"{desc} ({p.manufacturer})"
        out.append((by_id.get(p.device, p.device), desc))
    radioish = ("cp210", "ch340", "ch9102", "silicon labs", "usb serial", "uart", "esp32",
                "jtag", "heltec", "rak", "seeed", "lilygo")
    out.sort(key=lambda pd: not any(k in pd[1].lower() for k in radioish))
    return out


def find_zims(roots: list[Path] | None = None) -> list[Path]:
    """.zim files on USB sticks and in the usual places, biggest first."""
    if roots is None:
        user = os.environ.get("SUDO_USER") or os.environ.get("USER") or ""
        roots = [Path("/media"), Path("/mnt"), Path("/run/media"), Path.home()]
        if user:
            roots.insert(0, Path("/media") / user)
    found: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in ("*.zim", "*/*.zim", "*/*/*.zim"):
            try:
                found.update(p for p in root.glob(pattern) if p.is_file())
            except OSError:
                continue
    return sorted(found, key=lambda p: (-p.stat().st_size, str(p)))


def _size(p: Path) -> str:
    n = p.stat().st_size
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


# ---------------------------------------------------------------- questions

def _pick_port(ui, current: str | None, role: str) -> str | None:
    while True:
        ports = find_ports()
        choices = []
        for dev, desc in ports:
            label = f"{dev}  ({desc})" if desc else dev
            choices.append(ui.Choice(label, dev))
        if current and current not in [d for d, _ in ports]:
            choices.append(ui.Choice(f"{current}  (saved before; not plugged in right now)", current))
        choices.append(ui.Choice("Look again (plug the radio in first)", "__again"))
        choices.append(ui.Choice("Type a port name", "__type"))
        if role == "reader":
            choices.append(ui.Choice("Bluetooth instead (a T1000-E or other Bluetooth radio)", "__ble"))
            choices.append(ui.Choice("No radio yet (practice mode only)", "__none"))
        if not ports:
            ui.warn("no radio found. Plug it in with a data cable (not charge-only).")
        default = next((c.value for c in choices
                        if isinstance(c, ui.Choice) and c.value == current), None)
        answer = ui.choose("Which port is the radio on?", choices, default=default)
        if answer == "__again":
            continue
        if answer == "__type":
            return ui.text("Port (like COM12 or /dev/serial/by-id/...):", current or "") or None
        if answer == "__none":
            return None
        return answer


def _pick_ble(ui, current: str | None) -> str | None:
    """A Bluetooth radio's address ("" = any MeshCore radio), or None to go back to USB."""
    from .transport import scan_ble
    while True:
        found = []
        try:
            with ui.spin("looking for MeshCore radios over Bluetooth (about 8 seconds)..."):
                found = asyncio.run(scan_ble(8))
        except ImportError:
            ui.fail("Bluetooth needs the 'bleak' package: pip install bleak (or run the installer again)")
            return None
        except Exception as exc:
            ui.fail(f"couldn't scan for Bluetooth radios: {exc}")
            ui.note("Is Bluetooth switched on on this computer?")
        if not found:
            ui.warn("no MeshCore radio found. Switch it on, keep it close, and disconnect it from "
                    "the MeshCore phone app (a radio takes one Bluetooth connection at a time).")
        choices = [ui.Choice(f"{name}  ({addr}{f', signal {rssi} dBm' if rssi is not None else ''})", addr)
                   for addr, name, rssi in found]
        if current and current not in [a for a, _, _ in found]:
            choices.append(ui.Choice(f"{current}  (saved before; not seen right now)", current))
        choices += [ui.Choice("Scan again", "__again"),
                    ui.Choice("Any MeshCore radio nearby (the first one found)", ""),
                    ui.Choice("Type its address or name", "__type"),
                    ui.Choice("Use a USB cable instead", "__usb")]
        answer = ui.choose("Which Bluetooth radio?", choices)
        if answer == "__again":
            continue
        if answer == "__usb":
            return None
        if answer == "__type":
            return ui.text("Address or name (like MeshCore-Stern):", current or "")
        return answer


def _pick_link(ui, cfg: dict) -> "radiocheck.Link | None":
    """Ask how to reach the radio: USB port or Bluetooth. None = no radio yet."""
    while True:
        port = _pick_port(ui, cfg.get("port"), "reader")
        if port != "__ble":
            return radiocheck.Link(port=port) if port else None
        ble = _pick_ble(ui, cfg.get("ble"))
        if ble is not None:
            return radiocheck.Link(ble=ble, pin=cfg.get("pin"))


_last_failure: BaseException | None = None      # why the last _check_radio failed


def _check_radio(ui, link) -> bool:
    global _last_failure
    link = radiocheck.Link.of(link) if not isinstance(link, radiocheck.Link) else link
    where = f"Bluetooth ({link.ble or 'any MeshCore radio'})" if link.ble is not None else link.port
    lines: list[str] = []
    _last_failure = None
    try:
        with ui.spin(f"talking to the radio on {where}..."):
            asyncio.run(radiocheck.info(link, out=lines.append, pause_vault=False))
    except (Exception, SystemExit) as exc:
        _last_failure = exc
        ui.fail(f"couldn't use the radio on {where}: {exc}")
        if link.ble is None:
            ui.note("is another program (the MeshCore app, another terminal, the Seed Vault "
                    "service) using it?")
        return False
    for line in lines:
        ui.ok(line)
    return True


def _pick_zim(ui, current: str | None) -> str:
    while True:
        zims = find_zims()
        choices = [ui.Choice(f"{z}  ({_size(z)})", str(z)) for z in zims]
        if current and current not in [str(z) for z in zims]:
            choices.append(ui.Choice(f"{current}  (from the old settings; not found right now)", current))
        choices += [ui.Choice("Look again (plug the USB stick in first)", "__again"),
                    ui.Choice("Type the path", "__type")]
        if not zims:
            ui.warn("no .zim file found on any USB stick or in your home folder.")
        answer = ui.choose("Which library should the Seed Vault serve?", choices,
                           default=current if current in [c.value for c in choices
                                                          if isinstance(c, ui.Choice)] else None)
        if answer == "__again":
            continue
        if answer == "__type":
            return ui.path("Path to the .zim file:", current or "")
        return answer


# ---------------------------------------------------------------- the two setups

def _setup_reader(ui, cfg: dict, port: str | None, ask: bool, ble: str | None = None,
                  pin: str | None = None) -> int:
    if ask:
        link = _pick_link(ui, {**cfg, **({"port": port} if port else {})})
    elif ble is not None:
        link = radiocheck.Link(ble=ble, pin=pin)
    else:
        link = radiocheck.Link(port=port) if port else None
    if link is not None and not _check_radio(ui, link):
        from .transport import StalePairing
        if ask and link.ble is not None and isinstance(_last_failure, StalePairing):
            # The fix happens in the computer's Bluetooth settings; then a plain retry
            # makes the computer ask for the PIN itself.
            while ui.confirm("Forgotten the radio in your Bluetooth settings? Try again now?", True):
                if _check_radio(ui, link):
                    return _save_reader(ui, link, ask)
                if not isinstance(_last_failure, StalePairing):
                    break
        elif ask and link.ble is not None and sys.platform == "darwin":
            # macOS only takes a PIN through its own pop-up window, never from MOSS.
            ui.note("On a Mac the PIN goes into the window macOS pops up (the PIN is in the "
                    "MeshCore app's settings, often 123456). If no window appeared, forget the "
                    "radio in System Settings → Bluetooth first.")
            if ui.confirm("Try again?", True) and _check_radio(ui, link):
                return _save_reader(ui, link, ask)
        elif ask and link.ble is not None and not link.pin and ui.confirm(
                "Try again with a pairing PIN? (It's in the MeshCore app's settings; often 123456.)", True):
            link.pin = ui.text("PIN:", "123456")
            if _check_radio(ui, link):
                return _save_reader(ui, link, ask)
        if ask and ui.confirm("Pick a different radio?", True):
            return _setup_reader(ui, cfg, None, ask)
    return _save_reader(ui, link, ask)


def _save_reader(ui, link, ask: bool) -> int:
    if link is None:
        config.update(role="reader", port=None, ble=None, pin=None)
    elif link.ble is not None:
        config.update(role="reader", port=None, ble=link.ble, pin=link.pin)
    else:
        config.update(role="reader", port=link.port, ble=None, pin=None)
    ui.ok("saved: this machine is a reader, radio " + config.radio_label(config.load())
          if link else "saved: this machine is a reader, no radio yet")
    if ask:
        from . import pages
        if not pages.Authors().all() and ui.confirm(
                "Make an author identity now, for publishing community pages? "
                "(You can do it later from the menu.)", False):
            from .menu import new_identity
            new_identity()
    ui.header("Ready", "Type  [bold]moss[/]  for the menu, or  [bold]moss web[/]  to go "
                       "straight to the reader in your browser.")
    return 0


def _setup_vault(ui, cfg: dict, port: str | None, zim: str | None, ask: bool,
                 install_service: bool) -> int:
    if not service.available():
        ui.warn("this machine has no systemd, so the Seed Vault can't run as a background "
                "service here. You can still run it by hand: moss gateway --source FILE.zim")
        install_service = False
    old = service.read_env()
    if old:
        ui.ok("found your existing Seed Vault settings; keeping them as the defaults")
    port = port or old.get("MOSS_PORT") or cfg.get("port")
    zim = zim or old.get("MOSS_ZIM")
    extra = old.get("MOSS_EXTRA", "")
    if ask:
        port = _pick_port(ui, port, "vault")
        zim = _pick_zim(ui, zim)
        if extra:
            ui.note(f"extra options kept from before: {extra}")
    if not port or not zim:
        ui.fail("a Seed Vault needs both a radio port and a .zim library "
                "(--port and --zim when not asking)")
        return 1

    was_running = install_service and service.is_active()
    if was_running:
        ui.note("pausing the running Seed Vault to check the radio...")
        service.control("stop")
    if not _check_radio(ui, port):
        if was_running:
            service.control("start")
        return 1

    from .seal import VaultKeys
    keys_dir = config.moss_home() / "seed-vault"
    existed = (keys_dir / "ed25519.pem").exists()
    keys = VaultKeys.load_or_create(keys_dir)
    ui.ok(("kept the existing" if existed else "made new") +
          f" Seed Vault keys: fingerprint [bold]{keys.fingerprint}[/]")
    config.update(role="vault", port=port)

    if install_service:
        ui.note("installing the service (sudo may ask for your password)...")
        if not service.save_settings(port, zim, extra):
            ui.fail(f"couldn't write {service.ENV_FILE}")
            return 1
        if not service.install():
            ui.fail("couldn't install the service")
            return 1
        service.control("restart")
        ui.ok("Seed Vault service installed, starts on boot, running now"
              if service.is_active() else "service installed, but it didn't start: moss → Watch the log")
    ui.header("Seed Vault ready",
              f"library  {zim}",
              f"radio    {port}",
              f"keys     {keys.fingerprint}  (readers should see this same fingerprint)",
              "",
              "Type  [bold]moss[/]  for status, logs, start/stop and updates.")
    return 0


def run(role: str | None = None, port: str | None = None, zim: str | None = None,
        branch: str | None = None, yes: bool = False, install_service: bool = True,
        ble: str | None = None, pin: str | None = None) -> int:
    from . import ui
    cfg = config.load()
    ask = ui.interactive() and not yes
    if branch:
        if branch not in config.BRANCHES:
            raise SystemExit(f"branch must be one of {', '.join(config.BRANCHES)}")
        config.update(branch=branch)
    ui.header("MOSS setup", "Wikipedia and community pages over MeshCore radios.",
              f"installed in {config.install_dir()}")
    try:
        if role is None:
            role = cfg.get("role") or "reader"
            if ask:
                role = ui.choose("What is this machine?", [
                    ui.Choice("Reader       read Wikipedia and community pages over the radio", "reader"),
                    ui.Choice("Seed Vault   a Raspberry Pi holding the library", "vault"),
                ], default=role)
        if role not in ROLES:
            raise SystemExit(f"role must be reader or vault, not {role!r}")
        if role == "reader":
            return _setup_reader(ui, cfg, port, ask, ble, pin)
        return _setup_vault(ui, cfg, port, zim, ask, install_service)
    except ui.Back:
        ui.warn("setup stopped; run  moss setup  to finish it")
        return 1


def main_from_args(args) -> int:
    return run(args.role, args.port, args.zim, args.branch, args.yes, not args.no_service,
               args.ble, args.pin)


if __name__ == "__main__":                       # pragma: no cover
    sys.exit(run())
