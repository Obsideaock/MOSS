"""The .zim source: lookups, search, HTML cleanup, and a full radio round trip.

Builds a tiny ZIM from the fixtures (tests/make_test_zim.py), so it needs
`pip install libzim`; without it these tests are skipped.

    python -m unittest discover -s tests -v      (from the repo root)
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

try:
    import libzim  # noqa: F401
    HAVE_LIBZIM = True
except ImportError:
    HAVE_LIBZIM = False

from moss.cache import Cache                    # noqa: E402
from moss.client import MossClient, MossError   # noqa: E402
from moss.gateway import Gateway                # noqa: E402
from moss.sources import SourceError            # noqa: E402
from moss.transport import SimNetwork           # noqa: E402
from moss.zim import html_to_markdown, lead_blurb  # noqa: E402


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


class ConverterTests(unittest.TestCase):
    def test_keeps_reading_text_drops_clutter(self):
        html = """<html><head><title>T</title><style>p{}</style></head><body>
        <h1>T</h1>
        <div class="hatnote">For other uses, see T (disambiguation).</div>
        <table class="infobox"><tr><td>Born 1900</td></tr></table>
        <p>First <b>bold</b> sentence.<sup class="reference">[1]</sup> Area 5 km<sup>2</sup>.</p>
        <details><summary><h2>Life</h2></summary><section>
          <p>Lived.</p><ul><li>one</li><li>two<ul><li>nested</li></ul></li></ul>
        </section></details>
        <h2>References</h2><ol class="references"><li>Cite</li></ol>
        <h2>Legacy</h2><p>Remembered.</p>
        </body></html>"""
        md = html_to_markdown(html)
        self.assertIn("First **bold** sentence. Area 5 km².", md)
        self.assertIn("*For other uses, see T (disambiguation).*", md)   # hatnotes are kept
        self.assertIn(":::facts\nBorn 1900\n:::", md)                     # infobox -> key facts
        self.assertIn("## Life", md)
        self.assertIn("- one", md)
        self.assertIn("  - nested", md)
        self.assertIn("## Legacy\n\nRemembered.", md)
        for gone in ("[1]", "Cite", "References", "p{}"):
            self.assertNotIn(gone, md)
        self.assertNotIn("# T", md)              # the source adds its own title line

    def test_blurb_is_first_real_sentence(self):
        html = "<p></p><p>Short.</p><p>Cardboard is a generic term.<sup>[2]</sup> More text.</p>"
        self.assertEqual(lead_blurb(html), "Cardboard is a generic term.")


@unittest.skipUnless(HAVE_LIBZIM, "pip install libzim to run the .zim tests")
class ZimSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import contextlib
        import io
        from make_test_zim import build
        from moss.zim import ZimSource
        cls.tmp = Path(tempfile.mkdtemp())
        with contextlib.redirect_stdout(io.StringIO()):     # libzim's build chatter
            cls.path = build(cls.tmp / "mini.zim")
        cls.src = ZimSource(cls.path, max_bytes=None)

    def test_describe(self):
        d = self.src.describe()
        self.assertIn("full-text search", d)
        self.assertIn("title search", d)

    def test_search_finds_by_content_and_prefix(self):
        self.assertEqual(run(self.src.search("shrubbies band"))[0].title, "Shrubbies")
        self.assertEqual(run(self.src.search("cardb"))[0].title, "Cardboard")
        hit = run(self.src.search("pump crew"))[0]
        self.assertEqual(hit.title, "Boulder Water Status")
        self.assertTrue(hit.blurb)

    def test_search_shows_redirect_targets_once(self):
        titles = [r.title for r in run(self.src.search("Shrubbies"))]
        self.assertEqual(titles.count("Shrubbies"), 1)
        self.assertNotIn("The Shrubbies", titles)

    def test_nothing_found(self):
        self.assertEqual(run(self.src.search("qwxzv")), [])

    def test_article_lookup_variants(self):
        for asked in ("Shrubbies", "shrubbies", "The Shrubbies", "the shrubbies",
                      "Shrubbies (band)"):
            title, text = run(self.src.article(asked))
            self.assertEqual(title, "Shrubbies", asked)
            self.assertTrue(text.startswith("# Shrubbies\n\n*Source: Wikipedia, "
                                            "https://en.wikipedia.org/wiki/Shrubbies"))

    def test_article_is_clean(self):
        _, text = run(self.src.article("Cardboard"))
        self.assertIn(":::facts\nMaterial\tPaper\n:::", text)
        for gone in ("should not appear", "[1]", "var x"):
            self.assertNotIn(gone, text)

    def test_links_point_at_real_articles(self):
        _, text = run(self.src.article("Matrix (mathematics)"))
        # A redirect is followed to the article it lands on.
        self.assertIn("[[Cardboard|boxes of cardboard]]", text)
        self.assertIn("[[Shrubbies|the band's]]", text)
        # Not in this library, external, or a file: plain words.
        self.assertIn(" linear algebra and", text)
        self.assertIn("an external site", text)
        self.assertNotIn("[[an external", text)
        self.assertNotIn("File:", text)

    def test_math_comes_through_as_latex(self):
        _, text = run(self.src.article("Matrix (mathematics)"))
        self.assertIn("denoted $a_{ij}$ or $A_{i,j}$.", text)
        self.assertIn("$$\\mathbf {A} ={\\begin{bmatrix}1&2\\\\3&4\\end{bmatrix}}$$", text)
        self.assertIn("$$\\det(\\mathbf {A} )=ad-bc$$", text)
        self.assertIn("- Square: $m=n$", text)
        self.assertIn("costs \\$5", text)       # a real dollar sign isn't a formula
        self.assertNotIn("displaystyle", text)
        self.assertNotIn("Footer", text)         # Kiwix's licence footer stays home

    def test_table_goes_out_disassembled(self):
        _, text = run(self.src.article("List of best-selling video games"))
        self.assertIn(":::table Best-selling games\n!Rank\tTitle\tSales\tPlatform(s)\n"
                      "1\t*[[Minecraft]]*\t300,000,000\tMulti-platform\n"
                      "2\t*Grand Theft Auto V*\t210,000,000\t^\n", text)
        self.assertIn("4\t*Wii Sports*\t82,900,000\tWii · Wii U\n", text)
        self.assertIn("Sales as of 2026.\t<\t<\t<\n:::", text)
        for gone in ("Navigation box", "He\t", "[2]", "citation", "Footer"):
            self.assertNotIn(gone, text)

    def test_table_rebuilds(self):
        from moss import tables
        _, text = run(self.src.article("List of best-selling video games"))
        table = next(t for kind, t in tables.blocks(text) if kind == "table")
        platform = table.rows[1][3]
        self.assertEqual((platform.text, platform.rowspan), ("Multi-platform", 3))
        self.assertEqual(table.rows[5][0].colspan, 4)
        md = tables.to_markdown(table)
        self.assertEqual(md[0], "| Rank | Title | Sales | Platform(s) |")
        self.assertIn("| 2 | *Grand Theft Auto V* | 210,000,000 | Multi-platform |", md)

    def test_infobox_becomes_key_facts(self):
        _, text = run(self.src.article("Minecraft"))
        self.assertIn(":::facts\nDeveloper(s)\tMojang Studios\nReleased\t18 November 2011\n"
                      "!Sales\t<\n!Year\tCopies\n2014\t54 million\n2023\t300 million\n:::", text)
        self.assertNotIn("*Minecraft*\t", text)        # the box's own title row is dropped

    def test_formatting_scripts_lists_notes(self):
        _, text = run(self.src.article("List of best-selling video games"))
        self.assertIn("This is a list of the **best-selling video games** of all time. *Minecraft*", text)
        self.assertIn("above 300 million", text)                       # zero-width space gone
        self.assertIn("5 km², CO₂ 10⁻⁶, the 20th century, Aᵣ.", text)
        self.assertIn("1. Install it\n\n2. Play it\n\n  1. Mine\n\n  2. Craft\n\n3. Sell it", text)
        self.assertIn("*For best-selling consoles, see [[Cardboard]].*", text)
        self.assertIn("## See also\n\n- [[Shrubbies]]", text)
        _, text = run(self.src.article("Minecraft"))
        self.assertIn("*Main article: [[List of best-selling video games]]*", text)
        self.assertIn("**Minecraft** is a sandbox game. It costs \\$30.", text)

    def test_obsidian_tables(self):
        from moss.vault import to_obsidian
        _, text = run(self.src.article("Minecraft"))
        note = to_obsidian("Minecraft", text, 0)
        self.assertIn("> [!abstract]- Key facts\n> | Key facts |  |\n> |---|---|\n"
                      "> | Developer(s) | Mojang Studios |", note)

    def test_no_links_option(self):
        from moss.zim import ZimSource
        _, text = run(ZimSource(self.path, links=False).article("Matrix (mathematics)"))
        self.assertNotIn("[[", text)
        self.assertIn("boxes of cardboard", text)
        self.assertIn("$a_{ij}$", text)

    def test_missing_article(self):
        with self.assertRaises(SourceError):
            run(self.src.article("No Such Page Anywhere"))

    def test_obsidian_export_reads_zim_articles(self):
        from moss.vault import to_obsidian
        _, text = run(self.src.article("Cardboard"))
        note = to_obsidian("Cardboard", text, 0)
        self.assertIn("source: https://en.wikipedia.org/wiki/Cardboard", note)

    def test_round_trip_over_lossy_radio(self):
        async def go():
            net = SimNetwork(loss=0.2, seed=7)
            gw = Gateway(net.endpoint("gw"), self.src, gap=0.0, wait=2.0,
                         settle=0.1, log=lambda m: None)
            await gw.start()
            client = MossClient(net.endpoint("c"), Cache(tempfile.mkdtemp()),
                                quiet=0.4, request_timeout=1.0, fetch_timeout=5.0,
                                idle=8.0, manifest_retry=0.2, nack_spacing=0.05)
            try:
                found = await client.search("corrugated cardboard")
                page = await client.article(found.results[0].title)
                with self.assertRaises(MossError):
                    await client.article("No Such Page Anywhere")
                return found, page
            finally:
                await gw.stop()

        found, page = run(go())
        self.assertEqual(found.results[0].title, "Cardboard")
        _, direct = run(self.src.article("Cardboard"))
        self.assertEqual(page.text, direct)


if __name__ == "__main__":
    unittest.main()
