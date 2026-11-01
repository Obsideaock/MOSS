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


def _check_radio(ui, port: str) -> bool:
    lines: list[str] = []
    try:
        with ui.spin(f"talking to the radio on {port}..."):
            asyncio.run(radiocheck.info(port, out=lines.append, pause_vault=False))
    except (Exception, SystemExit) as exc:
        ui.fail(f"couldn't use the radio on {port}: {exc}")
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

def _setup_reader(ui, cfg: dict, port: str | None, ask: bool) -> int:
    if ask:
        port = _pick_port(ui, port or cfg.get("port"), "reader")
    if port:
        if not _check_radio(ui, port) and ask and ui.confirm("Pick a different port?", True):
            return _setup_reader(ui, cfg, None, ask)
    config.update(role="reader", port=port)
    ui.ok("saved: this machine is a reader" + (f", radio on {port}" if port else ", no radio yet"))
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
        branch: str | None = None, yes: bool = False, install_service: bool = True) -> int:
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
            return _setup_reader(ui, cfg, port, ask)
        return _setup_vault(ui, cfg, port, zim, ask, install_service)
    except ui.Back:
        ui.warn("setup stopped; run  moss setup  to finish it")
        return 1


def main_from_args(args) -> int:
    return run(args.role, args.port, args.zim, args.branch, args.yes, not args.no_service)


if __name__ == "__main__":                       # pragma: no cover
    sys.exit(run())
