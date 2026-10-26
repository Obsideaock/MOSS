"""End-to-end tests over a simulated radio. No hardware, no internet.

    python -m unittest discover -s tests -v      (from the repo root)
"""

import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from moss import protocol as P                       # noqa: E402
from moss.cache import Cache                         # noqa: E402
from moss.client import MossClient, MossError        # noqa: E402
from moss.gateway import Gateway                     # noqa: E402
from moss.sources import LocalSource                 # noqa: E402
from moss.transport import SimNetwork                # noqa: E402

FIXTURES = ROOT / "TestExamples"


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


class Rig:
    """A gateway and a client on one simulated channel."""

    def __init__(self, loss=0.0, seed=1, folder=FIXTURES, max_bytes=12000,
                 client_timing=None):
        self.net = SimNetwork(loss=loss, seed=seed)
        self.gtx, self.ctx = self.net.endpoint("gw"), self.net.endpoint("client")
        self.tmp = tempfile.mkdtemp()
        self.cache = Cache(self.tmp)
        self.log: list[str] = []
        # Scaled-down clocks: settle < quiet < wait, as in the real defaults.
        self.gateway = Gateway(self.gtx, LocalSource(folder, max_bytes), gap=0.0,
                               wait=2.0, settle=0.1, log=self.log.append)
        timing = dict(quiet=0.4, request_timeout=1.0, fetch_timeout=5.0, idle=8.0,
                      manifest_retry=0.2, nack_spacing=0.05)
        timing.update(client_timing or {})
        self.client = MossClient(self.ctx, self.cache, **timing)

    async def __aenter__(self):
        await self.gateway.start()
        return self

    async def __aexit__(self, *exc):
        await self.gateway.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class ProtocolTests(unittest.TestCase):
    def test_roundtrips(self):
        self.assertEqual(P.parse_request(P.build_request(7, 2, "Café ☕")), (7, 2, "Café ☕"))
        self.assertEqual(P.parse_status(P.build_status(9, P.ST_SENDING, 61)), (9, 3, 61))
        self.assertEqual(P.parse_error(P.build_error(3, "nope")), (3, "nope"))
        self.assertEqual(P.parse_nack(P.build_nack(5, [1, 2, 300])), (5, [1, 2, 300]))
        m = P.parse_manifest(P.build_manifest(4, 10, 1500, b"12345678", "Boulder", 1))
        self.assertEqual((m["tid"], m["total"], m["name"]), (4, 10, "Boulder"))

    def test_long_text_still_fits_and_stays_valid_utf8(self):
        raw = P.build_request(1, 1, "é" * 500)
        self.assertLessEqual(len(raw), P.MAX_DATAGRAM)
        P.parse_request(raw)[2].encode()
        self.assertLessEqual(len(P.build_error(1, "ü" * 500)), P.MAX_DATAGRAM)

    def test_results_roundtrip(self):
        rs = [P.SearchResult("A", "first"), P.SearchResult("B\tC", "multi\nline")]
        back = P.decode_results(P.encode_results(rs))
        self.assertEqual([r.title for r in back], ["A", "B C"])


class EndToEnd(unittest.TestCase):
    def test_search_then_read(self):
        async def go():
            async with Rig() as rig:
                found = await rig.client.search("shrubbies band")
                self.assertTrue(found.results)
                self.assertFalse(found.from_cache)
                top = found.results[0].title
                page = await rig.client.article(top)
                self.assertIn("English pop group", page.text)
                self.assertNotIn("[2]", page.text)           # citations stripped
                return rig.cache.list_articles()
        cached = run(go())
        self.assertEqual(len(cached), 1)

    def test_second_read_is_free(self):
        async def go():
            async with Rig() as rig:
                await rig.client.article("Shrubbies")
                sent_before = rig.net.sent
                again = await rig.client.article("shrubbies")   # different case
                self.assertTrue(again.from_cache)
                self.assertEqual(rig.net.sent, sent_before)     # nothing on the air
                refreshed = await rig.client.article("Shrubbies", refresh=True)
                self.assertFalse(refreshed.from_cache)
                self.assertGreater(rig.net.sent, sent_before)
        run(go())

    def test_search_cached_too(self):
        async def go():
            async with Rig() as rig:
                await rig.client.search("shrubbies")
                n = rig.net.sent
                hit = await rig.client.search("  Shrubbies ")
                self.assertTrue(hit.from_cache)
                self.assertEqual(rig.net.sent, n)
        run(go())

    def test_survives_heavy_packet_loss(self):
        async def go():
            async with Rig(loss=0.25, seed=7) as rig:
                page = await rig.client.article("Shrubbies")
                original = LocalSource(FIXTURES)
                _, expected = await original.article("Shrubbies")
                self.assertEqual(page.text, expected)
                return rig.net.dropped
        dropped = run(go())
        self.assertGreater(dropped, 0)

    def test_lost_manifest_is_recovered(self):
        async def go():
            async with Rig() as rig:
                rig.ctx.drop_next.append(lambda raw: raw[0] == P.MANIFEST)
                page = await rig.client.article("Shrubbies")
                self.assertIn("Shrubbies", page.text)
        run(go())

    def test_one_lost_done_does_not_stall_the_gateway(self):
        async def go():
            async with Rig() as rig:
                rig.ctx.drop_next.append(lambda raw: raw[0] == P.DONE)
                await rig.client.article("Shrubbies")
                await asyncio.sleep(0.3)                 # let the second DONE land
                return rig.log
        log = "\n".join(run(go()))
        self.assertIn("delivered", log)
        self.assertNotIn("no reply", log)

    def test_lost_first_request_is_resent(self):
        async def go():
            async with Rig() as rig:
                rig.gtx.drop_next.append(lambda raw: raw[0] == P.REQUEST)
                page = await rig.client.article("Shrubbies")
                self.assertIn("Shrubbies", page.text)
        run(go())

    def test_unknown_article_reports_error(self):
        async def go():
            async with Rig() as rig:
                with self.assertRaisesRegex(MossError, "no such article"):
                    await rig.client.article("Definitely Not A Page")
                with self.assertRaisesRegex(MossError, "nothing found"):
                    await rig.client.search("zzzzqqqq")
        run(go())

    def test_no_gateway_gives_up_clearly(self):
        async def go():
            net = SimNetwork()
            client = MossClient(net.endpoint(), Cache(tempfile.mkdtemp()),
                                request_timeout=0.2, request_attempts=2)
            with self.assertRaisesRegex(MossError, "no pulse"):
                await client.article("Anything")
        run(go())

    def test_progress_reports_stages_and_live_preview(self):
        async def go():
            async with Rig() as rig:
                events = []
                await rig.client.article("Shrubbies", on_progress=events.append)
                return events
        events = run(go())
        stages = [e.stage for e in events]
        self.assertIn("receiving", stages)
        self.assertEqual(stages[-1], "done")
        mid = [e for e in events if e.stage == "receiving" and e.preview]
        self.assertTrue(mid, "expected text to appear before the transfer finished")
        self.assertTrue(any(0 < len(e.have) < e.total for e in events))

    def test_two_clients_are_served_in_turn(self):
        async def go():
            async with Rig() as rig:
                second_tx = rig.net.endpoint("client2")
                second = MossClient(second_tx, Cache(tempfile.mkdtemp()),
                                    quiet=0.4, request_timeout=1.0,
                                    fetch_timeout=5.0, idle=8.0)
                a, b = await asyncio.gather(rig.client.article("Shrubbies"),
                                            second.article("Shrubbies"))
                self.assertEqual(a.text, b.text)
        run(go())

    def test_big_article_is_shortened(self):
        async def go():
            tmp = Path(tempfile.mkdtemp())
            body = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(200))
            (tmp / "big.md").write_text(f"# Big One\n\n{body}\n")
            async with Rig(folder=tmp, max_bytes=3000) as rig:
                page = await rig.client.article("Big One")
                self.assertLess(len(page.text.encode()), 3200)
                self.assertIn("shortened", page.text)
        run(go())


class WikipediaSourceTests(unittest.TestCase):
    """The network isn't available in tests, so feed it the response shapes
    Wikipedia's API returns and check what comes out."""

    def test_search_orders_by_relevance_and_trims_blurbs(self):
        from moss.sources import WikipediaSource
        src = WikipediaSource()
        src._get = lambda params: {"query": {"pages": {
            "22": {"title": "Second", "index": 2, "description": "later"},
            "11": {"title": "First", "index": 1, "description": "x" * 200},
            "33": {"title": "No blurb", "index": 3}}}}
        got = run(src.search("anything"))
        self.assertEqual([r.title for r in got], ["First", "Second", "No blurb"])
        self.assertEqual(len(got[0].blurb), 90)
        self.assertEqual(got[2].blurb, "")

    def test_article_drops_references_and_citations(self):
        import fetch_page
        from moss.sources import WikipediaSource
        body = ("Intro text.[1]\n\n== History ==\nSome history.[a]\n\n"
                "== References ==\n1. junk\n\n== External links ==\nlinks")
        original = fetch_page.fetch_extract
        fetch_page.fetch_extract = lambda title, lang: {"title": "Real Title", "extract": body}
        try:
            title, text = run(WikipediaSource(max_bytes=None).article("real_title"))
        finally:
            fetch_page.fetch_extract = original
        self.assertEqual(title, "Real Title")
        self.assertIn("## History", text)
        self.assertIn("Some history.", text)
        self.assertNotIn("junk", text)
        self.assertNotIn("[1]", text)
        self.assertNotIn("links", text)
        self.assertIn("CC BY-SA", text)                  # attribution stays

    def test_missing_article_becomes_a_short_error(self):
        import fetch_page
        from moss.sources import SourceError, WikipediaSource

        def boom(title, lang):
            raise SystemExit("no such article: Nope")
        original = fetch_page.fetch_extract
        fetch_page.fetch_extract = boom
        try:
            with self.assertRaisesRegex(SourceError, "no such article"):
                run(WikipediaSource().article("Nope"))
        finally:
            fetch_page.fetch_extract = original


class WebTests(unittest.TestCase):
    """The browser reader's HTTP side: a real server, a simulated radio."""

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        from moss import web
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.rig = Rig()
        asyncio.run_coroutine_threadsafe(self.rig.__aenter__(), self.loop).result(5)
        app = web.App(self.rig.client, self.loop, {"simulated": True, "label": "test"})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(app))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        asyncio.run_coroutine_threadsafe(self.rig.__aexit__(None, None, None), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)

    def call(self, path, body=None, method=None, headers=None):
        import json
        import urllib.error
        import urllib.request
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json"} if body is not None else {}
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def wait_job(self, job_id):
        import json
        import time
        for _ in range(100):
            _, raw = self.call(f"/api/job/{job_id}")
            job = json.loads(raw)
            if job["state"] != "running":
                return job
            time.sleep(0.1)
        self.fail("job never finished")

    def test_search_then_article_over_http(self):
        import json
        status, page = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn(b"moss reader", page)
        status, raw = self.call("/api/search", {"q": "shrubbies"})
        job = self.wait_job(json.loads(raw)["id"])
        self.assertEqual(job["state"], "done")
        title = job["result"]["results"][0]["title"]
        status, raw = self.call("/api/article", {"title": title})
        job = self.wait_job(json.loads(raw)["id"])
        self.assertEqual(job["state"], "done")
        self.assertIn("English pop group", job["result"]["text"])
        self.assertEqual(job["progress"]["stage"], "done")
        _, raw = self.call("/api/cached")
        self.assertEqual([a["title"] for a in json.loads(raw)], [title])
        status, raw = self.call(f"/api/cached?title={title}", method="DELETE")
        self.assertTrue(json.loads(raw)["removed"])

    def test_a_new_job_has_complete_progress_before_the_radio_speaks(self):
        import json
        _, raw = self.call("/api/search", {"q": "shrubbies"})
        p = json.loads(raw)["progress"]
        for key in ("stage", "have", "total", "elapsed", "attempt"):
            self.assertIn(key, p)                      # the page reads these on its first poll

    def test_guards_reject_foreign_hosts_and_non_json_posts(self):
        status, _ = self.call("/api/info", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.call("/api/search", headers={"Content-Type": "text/plain"}, body=None, method="POST")
        self.assertEqual(status, 415)
        status, _ = self.call("/api/search", {"q": "   "})
        self.assertEqual(status, 400)
        status, _ = self.call("/api/job/nope")
        self.assertEqual(status, 404)


class VaultTests(unittest.TestCase):
    def setUp(self):
        _, self.text = run(LocalSource(FIXTURES).article("Shrubbies"))

    def note(self, known):
        from moss.vault import to_obsidian
        return to_obsidian("Shrubbies", self.text, 1760000000, known)

    def test_properties_and_callout(self):
        note = self.note([])
        self.assertTrue(note.startswith("---\ntitle: \"Shrubbies\"\n"))
        self.assertIn("source: https://en.wikipedia.org/wiki/Shrubbies", note)
        self.assertIn('license: "CC BY-SA 4.0"', note)
        self.assertIn("  - wikipedia", note)
        self.assertIn("> [!info] From Wikipedia, received over radio", note)
        self.assertNotIn("\n# Shrubbies", note)            # the file name is the title
        self.assertNotIn("*Source:", note)                 # moved into the callout

    def test_first_mention_is_linked_once_and_only_in_prose(self):
        note = self.note(["North Sea Radio Orchestra", "Cardiacs", "Memphis in Texas"])
        body = note.split("---\n", 2)[2]
        self.assertEqual(body.count("[[North Sea Radio Orchestra]]"), 1)
        self.assertEqual(body.count("[[Cardiacs]]"), 1)
        # 'Memphis in Texas' also heads a section; headings must stay plain.
        self.assertIn("## Memphis in Texas", note)
        self.assertNotIn("## [[", note)

    def test_no_self_link_short_titles_or_double_wrapping(self):
        note = self.note(["Shrubbies", "EP", "Cardiacs", "Cardiacs members"])
        self.assertNotIn("[[Shrubbies]]", note)
        self.assertNotIn("[[EP]]", note)
        self.assertNotIn("[[[[", note)
        self.assertNotIn("[[Cardiacs]] members]]", note)

    def test_longer_title_wins_over_the_shorter_one_inside_it(self):
        from moss.vault import to_obsidian
        text = "# A\n\nWe went to Boulder, Colorado and then to Colorado again.\n"
        out = to_obsidian("A", text, None, ["Boulder, Colorado", "Colorado"])
        self.assertIn("[[Boulder, Colorado]] and then to [[Colorado]] again", out)

    def test_filename_safe_and_export_roundtrip(self):
        from moss.vault import export_vault, note_filename
        self.assertEqual(note_filename('AC/DC: "Back in Black"?'), "AC DC Back in Black")
        tmp = Path(tempfile.mkdtemp())
        try:
            cache = Cache(tmp / "cache")
            cache.put_article("Shrubbies", self.text)
            cache.put_article("Cardiacs", "# Cardiacs\n\nA band.\n")
            written = export_vault(cache, tmp / "vault")
            self.assertEqual(sorted(p.name for p in written),
                             ["Cardiacs.md", "Shrubbies.md"])
            self.assertIn("[[Cardiacs]]", (tmp / "vault/Wikipedia/Shrubbies.md").read_text())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CacheTests(unittest.TestCase):
    def test_aliases_and_forget(self):
        tmp = tempfile.mkdtemp()
        try:
            c = Cache(tmp)
            c.put_article("Boulder, Colorado", "text", aliases=["boulder"])
            self.assertEqual(len(c.list_articles()), 1)
            self.assertEqual(c.get_article("BOULDER")[1], "text")
            self.assertTrue(c.forget("boulder"))
            self.assertIsNone(c.get_article("Boulder, Colorado"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
