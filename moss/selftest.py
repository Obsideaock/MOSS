"""End-to-end test against a Seed Vault: real radios, or the simulator.

    moss selftest                       # this laptop's radio -> your Seed Vault
    moss selftest --sim TestExamples    # the same steps over a simulated, lossy radio

It does what a person would, over the air, and checks each answer:

    1. the Seed Vault answers a search
    2. it describes itself: same MOSS version, free storage
    3. a test page uploads
    4. it reads back by its address, signature checked, text identical
    5. a new version uploads
    6. the new version reads back
    7. the old version is refused when sent again (no replays)
    8. the page is deleted, and reads back as deleted

The test page is unlisted and comes from a throwaway anonymous identity, so
it never shows up in search or near your own pages; step 7 deletes it. It
uses 3 uploads of that throwaway identity's daily limit and about 10 spores
of the vault's daily upload budget. Your identities, saved articles and
seen-pages list aren't touched.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Awaitable, Callable

from . import pages as PG
from .client import MossClient, MossError

Out = Callable[[str], None]


async def run(client: MossClient, out: Out = print, query: str = "Colorado") -> bool:
    scratch = Path(tempfile.mkdtemp(prefix="moss-selftest-"))
    client.seen = PG.SeenVersions(scratch)          # keep test pages out of the real list
    who = PG.Authors(scratch).create(anonymous=True, note="selftest")
    name = PG.random_name()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    text1 = (f"# MOSS self-test\n\nA throwaway page made by `moss selftest` at {stamp}. "
             f"If you can read this, uploading and reading pages both work.\n")
    text2 = text1 + "\n## Version 2\n\nThis line only exists in the second version.\n"
    state: dict = {}
    results: list[tuple[str, bool, str, float]] = []

    async def step(label: str, fn: Callable[[], Awaitable[str]]) -> bool:
        out(f"… {label}")
        started = time.monotonic()
        try:
            detail = await fn()
            ok = True
        except (MossError, PG.PageError, AssertionError) as exc:
            detail, ok = str(exc) or type(exc).__name__, False
        took = time.monotonic() - started
        results.append((label, ok, detail, took))
        out(f"{'PASS' if ok else 'FAIL'}  {label}  ({took:.0f}s){': ' + detail if detail else ''}")
        return ok

    async def search():
        try:
            found = await client.search(query, refresh=True)
            return f"{len(found.results)} result(s) for {query!r}"
        except MossError as exc:
            if "nothing found" in str(exc):
                return f"answered (nothing found for {query!r})"
            raise

    async def specs():
        from . import PROTOCOL
        share, client.share_prints = client.share_prints, False     # no print trading in a test
        try:
            info = await client.specs()
        finally:
            client.share_prints = share
        assert int(info.get("protocol", 0)) >= PROTOCOL, \
            f"the vault runs MOSS {info.get('moss', '?')}; this reader needs the same: moss update on the Pi"
        room = info.get("storage") or {}
        return f"MOSS {info.get('moss')}, {room.get('free_mb', '?')} MB free for community pages"

    async def upload(version_text, label):
        page = who.sign(name, version_text, visibility=PG.UNLISTED)
        state[label] = page
        message = await client.upload(page.to_bytes())
        assert message.startswith("stored"), f"vault said: {message}"
        return f"{message}, {page.spores} spores"

    async def read(expect: PG.Page):
        got = await client.page(expect.address, refresh=True)
        assert got.page.version == expect.version, \
            f"got v{got.page.version}, expected v{expect.version}"
        assert got.page.text() == expect.text(), "text differs from what was uploaded"
        return f"v{got.page.version}, signature checked, text identical"

    async def replay():
        try:
            message = await client.upload(state["v1"].to_bytes())
        except MossError as exc:
            reason = str(exc)
            assert "older" in reason or "already has" in reason, f"no clear refusal: {reason}"
            return f"refused: {reason}"
        raise AssertionError(f"the vault accepted an old copy ({message})")

    async def delete():
        page = who.sign(name, None)
        state["gone"] = page
        message = await client.upload(page.to_bytes())
        got = await client.page(page.address, refresh=True)
        assert got.page.deleted, "it still reads back as a live page"
        return f"{message}; reads back as deleted"

    out(f"self-test page: moss:{who.id}/{name} (unlisted, throwaway identity)")
    ok = (await step("the Seed Vault answers a search", search)
          and await step("the Seed Vault describes itself", specs)
          and await step("upload a test page", lambda: upload(text1, "v1"))
          and await step("read it back by its address", lambda: read(state["v1"]))
          and await step("upload a new version", lambda: upload(text2, "v2"))
          and await step("read the new version back", lambda: read(state["v2"]))
          and await step("an old copy sent again is refused", replay)
          and await step("delete it, and it reads back as deleted", delete))
    total = sum(r[3] for r in results)
    out("")
    out(f"{'ALL PASSED' if ok else 'FAILED'}: {sum(r[1] for r in results)}/8 steps in {total:.0f}s")
    return ok
