"""`moss update`: get the newest MOSS from GitHub.

Follows one branch: main (stable) or experimental (testing). It only ever
fast-forwards, so it never throws away work: if this checkout has edits of
its own, they're set aside with `git stash` first (and it says so), and if
it has commits GitHub doesn't, it stops and explains instead of merging.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from . import config, service

Out = Callable[[str], None]


class UpdateError(Exception):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise UpdateError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def current(repo: Path | None = None) -> str:
    repo = repo or config.install_dir()
    try:
        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        commit = _git(repo, "log", "-1", "--format=%h %cd", "--date=format:%Y-%m-%d")
        return f"{branch} @ {commit}"
    except (UpdateError, OSError):
        return "unknown (not a git checkout)"


def update(branch: str | None = None, repo: Path | None = None, out: Out = print,
           install_deps: bool = True, restart: bool | None = None) -> bool:
    """Returns True if anything changed."""
    repo = repo or config.install_dir()
    cfg = config.load()
    branch = branch or cfg.get("branch") or "main"
    if not (repo / ".git").exists():
        raise UpdateError(f"{repo} isn't a git checkout, so it can't update itself. "
                          f"Reinstall with the one-line installer (see README).")
    out(f"checking GitHub for {branch}...")
    _git(repo, "fetch", "--quiet", "origin", branch)
    remote = f"origin/{branch}"

    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        label = f"set aside by moss update {time.strftime('%Y-%m-%d %H:%M')}"
        _git(repo, "stash", "push", "--quiet", "-m", label)
        out("this copy had its own edits; they're saved with `git stash` "
            "(get them back with: git stash pop):")
        for line in dirty.splitlines()[:10]:
            out(f"  {line}")

    before = _git(repo, "rev-parse", "HEAD")
    if _git(repo, "rev-parse", "--abbrev-ref", "HEAD") != branch:
        exists = _git(repo, "rev-parse", "--verify", "--quiet", branch, check=False)
        if exists:
            _git(repo, "checkout", "--quiet", branch)
        else:
            _git(repo, "checkout", "--quiet", "-b", branch, "--track", remote)
        out(f"switched to {branch}")
    ahead = _git(repo, "rev-list", "--count", f"{remote}..HEAD")
    if ahead != "0":
        raise UpdateError(f"this copy has {ahead} commit(s) on {branch} that GitHub doesn't. "
                          f"Push them (or move them to another branch) first; nothing was changed.")
    _git(repo, "merge", "--ff-only", "--quiet", remote)
    after = _git(repo, "rev-parse", "HEAD")
    if branch != cfg.get("branch"):
        config.update(branch=branch)

    if before == after:
        out(f"already up to date ({current(repo)})")
        return False
    log = _git(repo, "log", "--oneline", "--no-decorate", f"{before}..{after}")
    lines = log.splitlines()
    out(f"updated to {current(repo)}, {len(lines)} new change(s):")
    for line in lines[:15]:
        out(f"  {line}")
    if len(lines) > 15:
        out(f"  ...and {len(lines) - 15} more")
    if install_deps:
        out("updating Python packages...")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "-e", str(repo)])
        if r.returncode != 0:
            out("  pip had trouble; try: moss update again, or reinstall")
    if restart is None:
        restart = cfg.get("role") == "vault" and service.available() and service.is_active()
    if restart:
        out("restarting the Seed Vault...")
        service.control("restart")
    return True
