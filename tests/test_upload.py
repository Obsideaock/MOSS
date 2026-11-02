"""Stage 2: uploading community pages and reading them by address, over a
simulated radio that loses messages. No hardware needed.

For the same steps on real radios: moss selftest (or tools/hardware_test.py).

    python -m unittest discover -s tests -v      (from the repo root)
"""

import asyncio
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from moss import pages as PG                        # noqa: E402
from moss import protocol as P                      # noqa: E402
from moss import seal as S                          # noqa: E402
from moss import selftest                           # noqa: E402
from moss.cache import Cache                        # noqa: E402
from moss.client import MossClient, MossError       # noqa: E402
from moss.gateway import Gateway                    # noqa: E402
from moss.pagestore import Limits, PageStore        # noqa: E402
from moss.sources import LocalSource                # noqa: E402
from moss.transport import SimNetwork               # noqa: E402

FIXTURES = ROOT / "TestExamples"
SECRET_WORDS = "Lantern-quartz rendezvous by the old grain elevator"


def run(coro, limit=120):
    return asyncio.run(asyncio.wait_for(coro, limit))


class World:
    """A simulated mesh: one Seed Vault with a page store, one reader, and
    optionally an eavesdropper hearing everything."""

    def __init__(self, loss=0.0, seed=1, limits=None, plain=False, spy=False):
        self.home = Path(tempfile.mkdtemp())
        self.net = SimNetwork(loss=loss, seed=seed)
        self.heard: list[bytes] = []
        if spy:
            self.net.endpoint("spy").subscribe(self.heard.append)
        self.store = PageStore(self.home / "vault-pages", limits or Limits())
        self.keys = S.VaultKeys.load_or_create(self.home / "vault-keys")
        self.log: list[str] = []
        self.gateway = Gateway(self.net.endpoint("vault"), LocalSource(FIXTURES, None),
                               gap=0.0, wait=1.0, settle=0.1, keys=self.keys, pages=self.store,
                               upload_quiet=0.4, upload_idle=15, nack_spacing=0.05,
                               log=self.log.append)
        self.client = MossClient(
            self.net.endpoint("reader"), Cache(self.home / "cache"),
            keyring=None if plain else S.Keyring(self.home / "reader"), home=self.home / "reader",
            upload_gap=0.0, quiet=0.4, request_timeout=1.0, fetch_timeout=5, idle=8,
            manifest_retry=0.2, nack_spacing=0.05)
        self.author = PG.Authors(self.home / "author").create("Stern")

    async def __aenter__(self):
        await self.gateway.start()
        return self

    async def __aexit__(self, *exc):
        await self.gateway.stop()


def long_text(words: int = 2500, secret: str = "") -> str:
    rng = random.Random(4)
    vocab = "water sand gravel charcoal filter clean slow layer bucket cloth boil pump".split()
    body = " ".join(rng.choice(vocab) for _ in range(words))
    return f"# Water filters\n\n{secret}\n\n{body}.\n"


class RoundTripTests(unittest.TestCase):
    def test_upload_then_read_back(self):
        async def go():
            async with World() as w:
                page = w.author.sign("water-filters", long_text())
                msg = await w.client.upload(page.to_bytes())
                got = await w.client.page(page.address, refresh=True)
                return page, msg, got, w
        page, msg, got, w = run(go())
        self.assertEqual(msg, "stored v1")
        self.assertEqual(got.page.to_bytes(), page.to_bytes())
        self.assertEqual(w.store.get(page.address), page.to_bytes())
        self.assertGreater(page.spores, 10)

    def test_survives_heavy_loss(self):
        for loss, seed in ((0.25, 2), (0.25, 9), (0.3, 4)):
            async def go():
                async with World(loss=loss, seed=seed) as w:
                    page = w.author.sign("water-filters", long_text())
                    await w.client.upload(page.to_bytes())
                    got = await w.client.page(page.address, refresh=True)
                    return page, got
            page, got = run(go(), 240)
            self.assertEqual(got.page.text(), page.text(), f"loss {loss} seed {seed}")

    def test_upload_survives_a_third_lost(self):
        for seed in (3, 6, 11):
            async def go():
                async with World(loss=0.35, seed=seed) as w:
                    page = w.author.sign("water-filters", long_text())
                    msg = await w.client.upload(page.to_bytes())
                    return page, msg, w.store.get(page.address)
            page, msg, stored = run(go(), 240)
            self.assertEqual((msg, stored), ("stored v1", page.to_bytes()), f"seed {seed}")

    def test_cached_copy_used_unless_refreshed(self):
        async def go():
            async with World() as w:
                page = w.author.sign("water-filters", long_text(50))
                await w.client.upload(page.to_bytes())
                first = await w.client.page(page.address)
                second = await w.client.page(page.address)
                return first, second
        first, second = run(go())
        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)

    def test_missing_page(self):
        async def go():
            async with World() as w:
                with self.assertRaisesRegex(MossError, "doesn't have a page"):
                    await w.client.page(f"moss:{w.author.id}/nothing-here")
        run(go())

    def test_plain_reads_work_but_plain_uploads_dont(self):
        async def go():
            async with World(plain=True) as w:
                page = w.author.sign("water-filters", long_text(50))
                w.store.store(page.to_bytes(), page.address, page.version, len(page.to_bytes()))
                got = await w.client.page(page.address)
                with self.assertRaisesRegex(MossError, "encrypted"):
                    await w.client.upload(page.to_bytes())
                return got
        self.assertEqual(run(go()).page.version, 1)


class PrivacyTests(unittest.TestCase):
    def test_eavesdropper_learns_nothing_from_an_upload_or_read(self):
        async def go():
            async with World(spy=True) as w:
                page = w.author.sign("lantern-notes", long_text(300, SECRET_WORDS),
                                     visibility=PG.PUBLIC)
                await w.client.upload(page.to_bytes())
                await w.client.page(page.address, refresh=True)
                return page, w.heard
        page, heard = run(go())
        air = b"".join(heard)
        self.assertGreater(len(heard), 10)
        for secret in (SECRET_WORDS.encode(), b"lantern-notes", page.address.encode(),
                       page.record["author"].encode(), page.key, page.record["title"].encode(),
                       page.body[:40], page.record_bytes[:40]):
            self.assertNotIn(secret, air, f"{secret[:30]!r} went over the air in the clear")

    def test_vault_logs_dont_name_pages(self):
        async def go():
            async with World() as w:
                page = w.author.sign("lantern-notes", long_text(50))
                await w.client.upload(page.to_bytes())
                await w.client.page(page.address, refresh=True)
                return page, w.log
        page, log = run(go())
        text = "\n".join(log)
        self.assertNotIn("lantern", text)
        self.assertNotIn(page.address, text)
        self.assertIn("stored a community page", text)


class VersionTests(unittest.TestCase):
    def test_update_then_replay_refused(self):
        async def go():
            async with World() as w:
                v1 = w.author.sign("water-filters", long_text(50))
                v2 = w.author.sign("water-filters", long_text(60))
                await w.client.upload(v1.to_bytes())
                self.assertEqual(await w.client.upload(v2.to_bytes()), "stored v2")
                with self.assertRaisesRegex(MossError, "older copy"):
                    await w.client.upload(v1.to_bytes())
                again = await w.client.upload(v2.to_bytes())
                got = await w.client.page(v2.address, refresh=True)
                return again, got
        again, got = run(go())
        self.assertEqual(again, "already on this Seed Vault")
        self.assertEqual(got.page.version, 2)

    def test_deletion_replaces_the_page(self):
        async def go():
            async with World() as w:
                name = PG.random_name()
                v1 = w.author.sign(name, long_text(50), visibility=PG.UNLISTED)
                gone = w.author.sign(name, None)
                await w.client.upload(v1.to_bytes())
                await w.client.upload(gone.to_bytes())
                got = await w.client.page(v1.address, refresh=True)
                with self.assertRaises(MossError):
                    await w.client.upload(v1.to_bytes())       # can't bring it back by replay
                return got
        self.assertTrue(run(go()).page.deleted)

    def test_reader_refuses_an_older_copy_from_a_vault(self):
        """A vault (or a swapped USB stick) serving v1 after the reader saw v2."""
        async def go():
            async with World() as w:
                v1 = w.author.sign("water-filters", long_text(50))
                v2 = w.author.sign("water-filters", long_text(60))
                await w.client.upload(v1.to_bytes())
                await w.client.upload(v2.to_bytes())
                await w.client.page(v2.address, refresh=True)
                w.store._path(v1.address).write_bytes(v1.to_bytes())   # roll back on disk
                with self.assertRaisesRegex(MossError, "already seen"):
                    await w.client.page(v1.address, refresh=True)
        run(go())

    def test_tampered_page_on_the_vault_is_blight(self):
        async def go():
            async with World() as w:
                page = w.author.sign("water-filters", long_text(50))
                await w.client.upload(page.to_bytes())
                data = bytearray(page.to_bytes())
                data[-5] ^= 1
                w.store._path(page.address).write_bytes(bytes(data))
                with self.assertRaisesRegex(MossError, "blighted"):
                    await w.client.page(page.address, refresh=True)
        run(go())


class RefusalTests(unittest.TestCase):
    def upload_with(self, limits, pages_fn):
        async def go():
            async with World(limits=limits) as w:
                out = []
                for page in pages_fn(w):
                    try:
                        out.append(await w.client.upload(page.to_bytes()))
                    except MossError as exc:
                        out.append(f"refused: {exc}")
                return out, w
        return run(go())

    def test_too_big(self):
        out, w = self.upload_with(Limits(cap_spores=5),
                                  lambda w: [w.author.sign("big", long_text(3000))])
        self.assertRegex(out[0], r"refused: too big: \d+ spores, this Seed Vault's cap is 5")

    def test_per_author_limit(self):
        out, _ = self.upload_with(Limits(per_author_per_day=2), lambda w: [
            w.author.sign(f"page-{i}", long_text(30)) for i in range(3)])
        self.assertEqual(out[:2], ["stored v1", "stored v1"])
        self.assertIn("uploads for today are used up", out[2])

    def test_vault_budget_across_many_identities(self):
        def pages(w):
            authors = PG.Authors(w.home / "many")
            return [authors.create(f"Sock {i}").sign("hello", long_text(400)) for i in range(4)]
        out, _ = self.upload_with(Limits(spores_per_day=30), pages)
        self.assertTrue(out[0].startswith("stored"))
        self.assertTrue(any("airtime for today is used up" in o for o in out[1:]), out)

    def test_storage_full(self):
        out, _ = self.upload_with(Limits(storage_bytes=1500), lambda w: [
            w.author.sign("a", long_text(100)), w.author.sign("b", long_text(400))])
        self.assertEqual(out[0], "stored v1")
        self.assertIn("no room", out[1])

    def test_uploads_off(self):
        out, _ = self.upload_with(Limits(accept_uploads=False),
                                  lambda w: [w.author.sign("a", long_text(30))])
        self.assertIn("isn't taking uploads", out[0])

    def test_forged_page_refused_after_transfer(self):
        async def go():
            async with World() as w:
                page = w.author.sign("water-filters", long_text(50))
                data = bytearray(page.to_bytes())
                data[-3] ^= 1                                  # body no longer matches
                with self.assertRaises(MossError):
                    await w.client.upload(bytes(data))         # refused before sending
                # Bypass the reader's own check: offer the real size, send garbage.
                return await w.client.requester.upload(
                    bytes(data), f"{page.address} 1 {len(data)}", None, 0.0), w
        (stored, message), w = run(go())
        self.assertFalse(stored)
        self.assertIn("altered", message)
        self.assertIsNone(w.store.get(f"moss:{w.author.id}/water-filters"))

    def test_page_not_matching_its_offer(self):
        async def go():
            async with World() as w:
                page = w.author.sign("water-filters", long_text(50))
                other = w.author.sign("something-else", long_text(50))
                data = page.to_bytes()
                return await w.client.requester.upload(
                    data, f"{other.address} 1 {len(data)}", None, 0.0)
        stored, message = run(go())
        self.assertFalse(stored)
        self.assertIn("doesn't match", message)


class LostMessageTests(unittest.TestCase):
    def test_lost_acceptance_and_lost_result(self):
        async def go():
            w = World()
            reader = w.net.endpoints[-1]
            reader.drop_next += [lambda raw: raw[0] == S.SACCEPT,
                                 lambda raw: raw[0] == S.SACCEPT,
                                 lambda raw: raw[0] == S.SRESULT,
                                 lambda raw: raw[0] == S.SRESULT]
            async with w:
                page = w.author.sign("water-filters", long_text(200))
                return await w.client.upload(page.to_bytes())
        self.assertEqual(run(go()), "stored v1")

    def test_every_spore_lost_once(self):
        async def go():
            w = World()
            vault = w.net.endpoints[0]
            seen = set()

            def first_copy(raw):
                if raw[0] != P.UCHUNK:
                    return False
                if raw[3:5] in seen:
                    return False
                seen.add(raw[3:5])
                return True
            vault.drop_next += [first_copy] * 50
            async with w:
                page = w.author.sign("water-filters", long_text(400))
                return await w.client.upload(page.to_bytes())
        self.assertEqual(run(go()), "stored v1")


class SelfTestTests(unittest.TestCase):
    """The same end-to-end test the hardware check runs, over the simulator."""

    def test_selftest_passes(self):
        async def go():
            async with World(loss=0.15, seed=5) as w:
                out = []
                ok = await selftest.run(w.client, out.append, query="Shrubbies")
                return ok, out
        ok, out = run(go(), 300)
        self.assertTrue(ok, "\n".join(out))
        self.assertEqual(sum(1 for line in out if line.startswith("PASS")), 8)

    def test_selftest_catches_a_vault_that_accepts_replays(self):
        async def go():
            async with World() as w:
                w.store.check_offer = lambda *a: None          # a broken vault
                real_check = PG.check_newer
                PG.check_newer = lambda cur, new: "newer"
                try:
                    out = []
                    ok = await selftest.run(w.client, out.append, query="Shrubbies")
                finally:
                    PG.check_newer = real_check
                return ok, out
        ok, out = run(go(), 300)
        self.assertFalse(ok)
        self.assertTrue(any(line.startswith("FAIL") and "old copy" in line for line in out), out)


class OlderVaultTests(unittest.TestCase):
    def test_a_vault_without_uploads_gets_a_clear_message(self):
        async def go():
            async with World() as w:
                w.gateway.pages = None
                real = dict(P.KIND_NAMES)
                del P.KIND_NAMES[P.KIND_UPLOAD]           # what an older vault knows
                try:
                    page = w.author.sign("water-filters", long_text(30))
                    with self.assertRaisesRegex(MossError, "older MOSS"):
                        await w.client.upload(page.to_bytes())
                finally:
                    P.KIND_NAMES.clear()
                    P.KIND_NAMES.update(real)
        run(go())


class OutboxTests(unittest.TestCase):
    def test_newest_version_only_and_mark_sent(self):
        home = Path(tempfile.mkdtemp())
        a = PG.Authors(home).create("Stern")
        v1 = a.sign("x", "# X\n\none\n")
        PG.save_to_outbox(v1, home)
        v2 = a.sign("x", "# X\n\ntwo\n")
        PG.save_to_outbox(v2, home)
        other = a.sign("y", "# Y\n\nthree\n")
        PG.save_to_outbox(other, home)
        pending = PG.outbox_pending(home)
        self.assertEqual(sorted((p.address, p.version) for _, p in pending),
                         sorted([(v2.address, 2), (other.address, 1)]))
        PG.mark_sent(v2, home)
        self.assertEqual([p.address for _, p in PG.outbox_pending(home)], [other.address])
        self.assertEqual(len(list((home / "outbox" / "sent").glob("*.mosspage"))), 2)


if __name__ == "__main__":
    unittest.main()
