"""Stage 3: finding pages, vault specs, spore prints, routing, dedicated link.

All over the simulated radio or the fake MeshCore radio; no hardware.
On real radios: moss selftest, moss vault-info, moss radio-link.

    python -m unittest discover -s tests -v      (from the repo root)
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_radio import Factory                              # noqa: E402
from moss import PROTOCOL                                   # noqa: E402
from moss import pages as PG                                # noqa: E402
from moss import protocol as P                              # noqa: E402
from moss import radiocheck                                 # noqa: E402
from moss import seal as S                                  # noqa: E402
from moss import sporeprint as SP                           # noqa: E402
from moss.client import MossError                           # noqa: E402
from moss.pagestore import PageStore                        # noqa: E402
from moss.social import Social                              # noqa: E402
from moss.transport import MeshCoreTransport                # noqa: E402
from test_upload import World, run                          # noqa: E402


def put(store: PageStore, page: PG.Page) -> None:
    data = page.to_bytes()
    store.store(data, page.address, page.version, len(data))


# ---------------------------------------------------------------- vault specs

class SpecsTests(unittest.TestCase):
    def test_specs_say_what_they_should(self):
        async def go():
            async with World() as w:
                w.gateway.library = "Wikipedia (test)"
                put(w.store, w.author.sign("water", "# Water\n\nPublic page.\n"))
                return await w.client.specs()
        s = run(go())
        self.assertEqual(s["protocol"], PROTOCOL)
        self.assertEqual(s["library"], "Wikipedia (test)")
        self.assertEqual(s["community"]["public_pages"], 1)
        for k in ("free_mb", "used_mb", "cap_mb", "disk_free_mb"):
            self.assertIn(k, s["storage"])
        self.assertGreater(s["storage"]["free_mb"], 0)

    def test_specs_never_say_anything_private(self):
        async def go():
            async with World() as w:
                public = w.author.sign("water", "# Water\n\nPublic page.\n")
                secret = w.author.sign(PG.random_name(), "# Clinic\n\nUnlisted.\n", visibility=PG.UNLISTED)
                await w.client.upload(public.to_bytes())
                await w.client.upload(secret.to_bytes())
                await w.client.page(secret.address, refresh=True)      # a read happens
                specs = await w.client.specs()
                return specs, secret, w
        specs, secret, w = run(go())
        text = json.dumps(specs)
        self.assertEqual(specs["community"]["public_pages"], 1, "unlisted pages must not be counted")
        for private in (secret.address, PG.parse_address(secret.address)[1], w.author.id,
                        w.author.fingerprint, "Stern", "Clinic"):
            self.assertNotIn(private, text)
        allowed = {"name", "fingerprint", "moss", "protocol", "library", "encrypted", "community",
                   "storage", "spore_print", "print_exchange"}
        self.assertLessEqual(set(specs), allowed)

    def test_older_vault_gets_a_clear_message_and_search_still_works(self):
        async def go():
            async with World() as w:
                real = dict(P.KIND_NAMES)
                for k in (P.KIND_SPECS, P.KIND_SEARCH_ALL, P.KIND_PRINT):
                    del P.KIND_NAMES[k]                   # what a vault before this version knows
                try:
                    found = await w.client.search("Shrubbies", refresh=True)
                    with self.assertRaisesRegex(MossError, "older MOSS.*moss update"):
                        await w.client.specs()
                finally:
                    P.KIND_NAMES.clear()
                    P.KIND_NAMES.update(real)
                return found
        found = run(go())
        self.assertTrue(found.results, "falls back to a Wikipedia-only search")
        self.assertEqual(found.community, [])

    def test_hello_carries_the_version(self):
        keys = S.VaultKeys.load_or_create(Path(tempfile.mkdtemp()))
        hello = keys.hello()
        self.assertEqual(S.verify_hello(hello)[2], "Seed Vault")
        from moss import __version__
        self.assertEqual(S.hello_version(hello), (__version__, PROTOCOL))
        old = hello[:129] + b"Seed Vault"                  # a vault from before versions
        self.assertIsNone(S.hello_version(old))
        self.assertEqual(S.verify_hello(old)[2], "Seed Vault")


# ---------------------------------------------------------------- search and ranking

class SearchTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.store = PageStore(self.home / "pages")
        authors = PG.Authors(self.home / "a")
        self.a, self.b, self.c, self.d = (authors.create(n) for n in ("Ann", "Bo", "Cy", "Dee"))

    def test_linked_page_ranks_higher(self):
        x = self.a.sign("filters-x", "# Water filters X\n\nSand water filter guide.\n")
        y = self.a.sign("filters-y", "# Water filters Y\n\nSand water filter guide.\n")
        for p in (x, y):
            put(self.store, p)
        put(self.store, self.b.sign("links", f"# Bo\n\nGood: {x.address}\n"))
        put(self.store, self.c.sign("links", f"# Cy\n\nAlso {x.address}\n"))
        found, _ = self.store.search("water filter")
        self.assertEqual(found[0]["a"], x.address)

    def test_link_farm_counts_once_and_self_links_count_nothing(self):
        self.store.limits.per_author_per_day = 99
        x = self.a.sign("filters-x", "# Water filters X\n\nSand water filter guide.\n")
        y = self.b.sign("filters-y", "# Water filters Y\n\nSand water filter guide.\n")
        put(self.store, x)
        put(self.store, y)
        put(self.store, self.c.sign("one", f"# C\n\n{x.address}\n"))
        put(self.store, self.d.sign("two", f"# D\n\n{x.address}\n"))
        for i in range(5):                                 # one author, many pages, all for y
            put(self.store, self.b.sign(f"farm-{i}", f"# Farm {i}\n\n{y.address}\n"))
        self.store.limits.per_author_per_day = 99
        found, _ = self.store.search("water filter")
        self.assertEqual(found[0]["a"], x.address, "two real fans beat one author's link farm")

    def test_unlisted_and_deleted_never_appear(self):
        put(self.store, self.a.sign("water", "# Water\n\nwater\n"))
        put(self.store, self.a.sign(PG.random_name(), "# Water secret\n\nwater\n", visibility=PG.UNLISTED))
        gone = self.b.sign("water-b", "# Water B\n\nwater\n")
        put(self.store, gone)
        put(self.store, self.b.sign("water-b", None))
        found, _ = self.store.search("water")
        self.assertEqual([r["t"] for r in found], ["Water"])
        self.assertEqual(self.store.public_count(), 1)

    def test_author_listing_and_paging(self):
        self.store.limits.per_author_per_day = 99
        for i in range(13):
            put(self.store, self.a.sign(f"note-{i}", f"# Note {i}\n\nnotes\n"))
        first, more = self.store.search("notes")
        self.assertEqual((len(first), more), (10, True))
        rest, more = self.store.search("notes", offset=10)
        self.assertEqual((len(rest), more), (3, False))
        mine, _ = self.store.search(f"author:{self.a.id}")
        self.assertEqual(len(mine), 10)
        self.assertEqual(self.store.search(f"author:{self.b.id}")[0], [])

    def test_downloads_count_public_pages_only(self):
        pub = self.a.sign("water", "# Water\n\nwater\n")
        unl = self.a.sign(PG.random_name(), "# W\n\nwater\n", visibility=PG.UNLISTED)
        put(self.store, pub)
        put(self.store, unl)
        self.store.count_download(pub.address)
        self.store.count_download(unl.address)
        self.assertEqual(self.store.downloads(), {pub.address: 1})

    def test_combined_search_over_the_air(self):
        async def go():
            async with World() as w:
                page = w.author.sign("shrub-care", "# Shrub care\n\nShrubbies tips.\n")
                await w.client.upload(page.to_bytes())
                return await w.client.search("Shrubbies", refresh=True), page
        found, page = run(go())
        self.assertTrue(found.results, "Wikipedia results")
        self.assertEqual([c["a"] for c in found.community], [page.address])


class LocalRankingTests(unittest.TestCase):
    def test_follows_move_results_up_but_never_leave_the_device(self):
        async def go():
            async with World() as w:
                other = PG.Authors(w.home / "o").create("Other")
                mine = w.author.sign("shrub-a", "# Shrubbies A\n\nShrubbies.\n")
                theirs = other.sign("shrub-b", "# Shrubbies B\n\nShrubbies. Shrubbies. Shrubbies.\n")
                await w.client.upload(mine.to_bytes())
                await w.client.upload(theirs.to_bytes())
                queries = []
                real = w.gateway._on_request

                async def spy(rid, kind, query, *a, **k):
                    queries.append(query)
                    return await real(rid, kind, query, *a, **k)
                w.gateway._on_request = spy
                w.client.social.follow(other.id, "Other")
                found = await w.client.search("shrubbies", refresh=True)
                return found, other, queries
        found, other, queries = run(go())
        self.assertTrue(found.community[0]["a"].startswith(f"moss:{other.id}/"))
        self.assertTrue(all(other.id not in q and "Other" not in q for q in queries), queries)

    def test_rerank_order(self):
        s = Social(Path(tempfile.mkdtemp()))
        aid = "b" * 26
        s.follow(aid, "B")
        rows = [{"a": f"moss:{'a' * 26}/one"}, {"a": f"moss:{aid}/two"}, {"a": f"moss:{'c' * 26}/three"}]
        linked_text = [f"see moss:{'c' * 26}/three"]
        out = [r["a"].rsplit("/", 1)[1] for r in s.rerank(rows, linked_text)]
        self.assertEqual(out, ["two", "three", "one"])


# ---------------------------------------------------------------- spore prints

class SporePrintTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.atlas = S.VaultKeys.load_or_create(base / "atlas", "ATLAS Seed Vault")
        self.laf = S.VaultKeys.load_or_create(base / "laf", "Lafayette")
        self.lon = S.VaultKeys.load_or_create(base / "lon", "Longmont")

    def test_chain_grows_and_verifies(self):
        root = SP.make_root(self.atlas, "The original Seed Vault, ATLAS, CU Boulder")
        two = SP.adopt(SP.adopt(root, self.laf), self.lon)
        SP.verify(two)
        s = SP.summary(two)
        self.assertEqual((s["hops"], s["chain"]), (2, ["ATLAS Seed Vault", "Lafayette", "Longmont"]))
        self.assertIn("2 hops from the original", SP.describe(s))

    def test_forgeries_are_refused(self):
        root = SP.make_root(self.atlas, "The original")
        one = SP.adopt(root, self.laf)
        bad = json.loads(SP.to_bytes(one))
        bad["root"]["note"] = "The REAL original"                       # edit the root
        with self.assertRaises(SP.PrintError):
            SP.verify(bad)
        cut = json.loads(SP.to_bytes(SP.adopt(one, self.lon)))
        del cut["links"][0]                                              # drop a link
        with self.assertRaises(SP.PrintError):
            SP.verify(cut)
        swapped = json.loads(SP.to_bytes(one))
        swapped["links"][0]["name"] = "Boulder (honest)"                 # rename a hop
        with self.assertRaises(SP.PrintError):
            SP.verify(swapped)
        with self.assertRaises(SP.PrintError):
            SP.adopt(one, self.laf)                                      # can't join twice
        fake_root = SP.make_root(self.laf, "The original Seed Vault, ATLAS, CU Boulder")
        self.assertNotEqual(SP.summary(fake_root)["origin"], SP.summary(root)["origin"],
                            "copying the note doesn't copy the fingerprint")

    def test_prints_never_name_readers(self):
        reader = PG.Authors(Path(tempfile.mkdtemp())).create("Stern")
        one = SP.adopt(SP.make_root(self.atlas, "The original"), self.laf)
        blob = SP.to_bytes(one).decode()
        self.assertNotIn(reader.id, blob)
        self.assertEqual(one["links"][0]["via"], "reader")
        self.assertEqual(set(one["links"][0]) - {"sig"}, {"from", "to", "x", "name", "via", "time"})

    def test_reader_carries_a_print_between_vaults(self):
        """Visit the original, then a vault with no print: the second vault
        adopts it, one hop from the original."""
        async def visit(home, vault_keys, vault_print):
            w = World()
            w.client.prints = SP.CarriedPrints(home)
            w.gateway.keys = vault_keys
            w.gateway.spore_print = vault_print
            async with w:
                return await w.client.specs()

        base = Path(tempfile.mkdtemp())
        reader_home = base / "reader"
        atlas_print = SP.VaultPrint(base / "atlas-vault")
        atlas_print.start_original(self.atlas, "The original Seed Vault, ATLAS, CU Boulder")
        laf_print = SP.VaultPrint(base / "laf-vault")

        first = run(visit(reader_home, self.atlas, atlas_print))
        self.assertEqual(first["print_exchange"], "picked up its spore print")
        second = run(visit(reader_home, self.laf, laf_print))
        self.assertIn("gave it a spore print", second["print_exchange"])
        adopted = laf_print.get()
        self.assertEqual(SP.summary(adopted)["hops"], 1)
        self.assertEqual(SP.summary(adopted)["chain"], ["ATLAS Seed Vault", "Lafayette"])
        third = run(visit(reader_home, self.laf, laf_print))
        self.assertEqual(third["print_exchange"], "already carrying that spore print")
        mine = SP.CarriedPrints(reader_home).mine()
        self.assertEqual(SP.summary(mine)["hops"], 0, "the reader's own print: its first vault")

    def test_vault_keeps_its_first_print(self):
        base = Path(tempfile.mkdtemp())
        vp = SP.VaultPrint(base)
        a = SP.make_root(self.atlas, "Original A")
        b = SP.make_root(self.lon, "Original B")
        vp.offered(SP.to_bytes(a), self.laf)
        self.assertIn("already has", vp.offered(SP.to_bytes(b), self.laf))
        self.assertEqual(SP.summary(vp.get())["note"], "Original A")


# ---------------------------------------------------------------- routing and the dedicated link

class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.factory = Factory()
        self.addCleanup(self.factory.install())

    def open(self, route=True):
        return MeshCoreTransport.open("COM12", 115200, route=route, log=lambda m: None)

    def test_learns_the_way_back_and_sends_along_it(self):
        async def go():
            tx = await self.open()
            heard = []
            tx.subscribe(heard.append)
            radio = self.factory.radios[-1]
            await radio.heard_flood(tx.channel, b"\x0arequest-bytes", path=b"\x11\x22\x33")
            for _ in range(100):
                if heard:
                    break
                await asyncio.sleep(0.02)
            back = tx.return_path(heard[0])
            await tx.send(b"\x02spore", path=back)
            await tx.send(b"\x02flooded")
            await tx.close()
            return back, radio.paths, tx.routed_sends
        back, paths, routed = run(go())
        self.assertEqual(back, b"\x33\x22\x11", "reversed: the way here, backwards")
        self.assertEqual(paths, [b"\x33\x22\x11", None])
        self.assertEqual(routed, 1)

    def test_off_by_default_and_safe_fallbacks(self):
        async def go(route, hash_size=1):
            tx = await self.open(route)
            heard = []
            tx.subscribe(heard.append)
            radio = self.factory.radios[-1]
            await radio.heard_flood(tx.channel, b"\x0areq", path=b"\x11\x22", hash_size=hash_size)
            for _ in range(100):
                if heard:
                    break
                await asyncio.sleep(0.02)
            back = tx.return_path(heard[0])
            await tx.close()
            return back
        self.assertIsNone(run(go(route=False)), "routing is opt-in")
        self.assertIsNone(run(go(route=True, hash_size=2)), "multi-byte repeater ids: flood")

    def test_radio_refusing_a_route_floods_instead(self):
        self.factory.refuse_routes = True

        async def go():
            tx = await self.open()
            await tx.send(b"\x02spore", path=b"\x11")
            await tx.close()
            return self.factory.radios[-1]
        radio = run(go())
        self.assertEqual(radio.sent, [(radio.sent[0][0], b"\x02spore")])
        self.assertEqual(radio.paths, [None])

    def test_other_channels_are_never_matched(self):
        async def go():
            tx = await self.open()
            radio = self.factory.radios[-1]
            await radio.heard_flood(0, b"\x0apublic", path=b"\x44")          # Public's key, not #moss
            await asyncio.sleep(0.1)
            await tx.close()
            return tx
        tx = run(go())
        self.assertEqual(tx._heard_log, [])


class DedicatedLinkTests(unittest.TestCase):
    def setUp(self):
        self.factory = Factory()
        self.addCleanup(self.factory.install())

    def test_settings_are_checked(self):
        self.assertEqual(radiocheck.check_link(*radiocheck.PUBLIC_US), [])
        self.assertTrue(radiocheck.check_link(915.0, 63, 7, 5))
        self.assertTrue(radiocheck.check_link(915.0, 62.5, 13, 5))
        self.assertTrue(any(p.startswith("note:") for p in radiocheck.check_link(868.0, 62.5, 7, 5)))

    def test_sets_the_radio(self):
        out = []
        ok = asyncio.run(radiocheck.set_link("COM12", 915.0, 125.0, 9, 5, out=out.append))
        self.assertTrue(ok)
        info = self.factory.radios[-1].self_info
        self.assertEqual((info["radio_freq"], info["radio_bw"], info["radio_sf"], info["radio_cr"]),
                         (915.0, 125.0, 9, 5))


if __name__ == "__main__":
    unittest.main()
