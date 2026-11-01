"""What this machine remembers between runs: ~/.moss/config.json.

Written by `moss setup` (and the menu's Settings), read at start-up so
`--port COM12` and friends don't have to be typed every time. A flag on
the command line always wins over the saved value.

    role      "reader" or "vault"
    port      the radio's serial port (COM12, /dev/serial/by-id/...)
    channel   the hashtag channel, #moss unless changed
    scope     MeshCore region to keep flooded traffic inside, or null
    branch    which GitHub branch `moss update` follows: main or experimental
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {"role": None, "port": None, "channel": "#moss", "scope": None, "branch": "main"}
BRANCHES = ("main", "experimental")


def moss_home() -> Path:
    """~/.moss, or $MOSS_HOME if set (handy for tests and second setups)."""
    return Path(os.environ.get("MOSS_HOME") or Path.home() / ".moss").expanduser()


def install_dir() -> Path:
    """The MOSS checkout this code is running from."""
    return Path(__file__).resolve().parent.parent


def path() -> Path:
    return moss_home() / "config.json"


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        saved = json.loads(path().read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def update(**changes) -> dict:
    cfg = load()
    cfg.update(changes)
    save(cfg)
    return cfg
