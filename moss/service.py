"""The Seed Vault's background service (systemd, on the Pi).

Settings live in /etc/moss/gateway.env (MOSS_PORT, MOSS_ZIM, MOSS_EXTRA),
the unit in /etc/systemd/system/moss-gateway.service. Changing either
needs root, so those steps run through sudo and may ask for a password.
"""

from __future__ import annotations

import getpass
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from .config import install_dir

NAME = "moss-gateway"
ENV_FILE = Path("/etc/moss/gateway.env")
UNIT_FILE = Path(f"/etc/systemd/system/{NAME}.service")


def available() -> bool:
    return shutil.which("systemctl") is not None


def _sudo(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    if os.geteuid() != 0:
        cmd = ["sudo", *cmd]
    return subprocess.run(cmd, **kw)


def installed() -> bool:
    return UNIT_FILE.exists()


def is_active() -> bool:
    if not available():
        return False
    r = subprocess.run(["systemctl", "is-active", "--quiet", NAME])
    return r.returncode == 0


def since() -> str:
    r = subprocess.run(["systemctl", "show", NAME, "--property=ActiveEnterTimestamp", "--value"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def control(action: str) -> bool:
    """start, stop or restart. True if systemctl was happy."""
    return _sudo(["systemctl", action, NAME]).returncode == 0


def recent_log(lines: int = 15) -> str:
    r = subprocess.run(["journalctl", "-u", NAME, "-n", str(lines), "--no-pager", "-o", "cat"],
                       capture_output=True, text=True)
    return r.stdout.strip() or r.stderr.strip()


def follow_log() -> None:
    """Show the log live until Ctrl+C."""
    try:
        subprocess.run(["journalctl", "-u", NAME, "-f", "-n", "20", "-o", "cat"])
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- settings file

def read_env(path: Path = ENV_FILE) -> dict:
    """MOSS_PORT, MOSS_ZIM, MOSS_EXTRA from the settings file (empty if none)."""
    out = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def render_env(port: str, zim: str, extra: str = "", old_text: str | None = None) -> str:
    """The settings file with these values, keeping any comments and other
    lines from the old file."""
    values = {"MOSS_PORT": port, "MOSS_ZIM": zim, "MOSS_EXTRA": extra}
    if old_text is None:
        old_text = (install_dir() / "deploy" / "gateway.env").read_text(encoding="utf-8")
    out, done = [], set()
    for line in old_text.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in values:
            out.append(f"{key}={values[key]}")
            done.add(key)
        else:
            out.append(line)
    for k, v in values.items():
        if k not in done:
            out.append(f"{k}={v}")
    return "\n".join(out) + "\n"


def write_root_file(path: Path, text: str) -> bool:
    _sudo(["mkdir", "-p", str(path.parent)])
    r = _sudo(["tee", str(path)], input=text, text=True, stdout=subprocess.DEVNULL)
    return r.returncode == 0


def save_settings(port: str, zim: str, extra: str = "") -> bool:
    try:
        old = ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        old = None
    return write_root_file(ENV_FILE, render_env(port, zim, extra, old))


def install(user: str | None = None) -> bool:
    """Write the unit (from deploy/), let the user use serial ports, enable at boot."""
    user = user or getpass.getuser()
    repo = install_dir()
    unit = (repo / "deploy" / f"{NAME}.service").read_text(encoding="utf-8")
    unit = unit.replace("@USER@", user).replace("@REPO@", str(repo))
    ok = write_root_file(UNIT_FILE, unit)
    _sudo(["usermod", "-aG", "dialout", user])
    ok = ok and _sudo(["systemctl", "daemon-reload"]).returncode == 0
    ok = ok and _sudo(["systemctl", "enable", NAME], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode == 0
    return ok


def command_line() -> str:
    """How the service runs MOSS, for showing to people."""
    env = read_env()
    return " ".join(["moss gateway", "--port", shlex.quote(env.get("MOSS_PORT", "?")),
                     "--source", shlex.quote(env.get("MOSS_ZIM", "?")), env.get("MOSS_EXTRA", "")]).strip()
