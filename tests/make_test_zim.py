"""Build a tiny Wikipedia-style .zim for testing, from the markdown fixtures.

    python tests/make_test_zim.py                       # -> TestExamples/mini_wikipedia.zim
    python tests/make_test_zim.py --out somewhere.zim

The HTML imitates what Kiwix's Wikipedia ZIMs contain (collapsible
sections, an infobox table, footnote markers, a References list, a hatnote)
so the converter is tested against the clutter it will really meet. It has
a full-text index and a title index, like the real 57 GB file, plus a few
redirects. Use it to try the gateway without copying the big file around:

    python -m moss web --sim TestExamples/mini_wikipedia.zim
"""

from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "TestExamples"


def md_to_kiwix_html(title: str, md: str) -> str:
    """Wrap a markdown fixture in Kiwix/mwoffliner-shaped HTML."""
    lines = md.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    lead, sections, current = [], [], None
    para: list[str] = []

    def flush(target):
        if para:
            text = " ".join(para)
            if text.startswith("- "):
                items = "".join(f"<li>{html.escape(i.strip())}</li>"
                                for i in text.split("- ") if i.strip())
                target.append(f"<ul>{items}</ul>")
            elif not text.startswith("*Source:"):
                # A footnote marker after the first sentence, like the real thing.
                text = html.escape(text)
                text = re.sub(r"\. ", '.<sup class="mw-ref reference"><a href="#cite_note-1">'
                                      '<span class="mw-reflink-text">[1]</span></a></sup> ',
                              text, count=1)
                target.append(f"<p>{text}</p>")
            para.clear()

    for line in lines:
        m = re.match(r"^(#{2,4})\s+(.*)$", line)
        if m:
            flush(current["body"] if current else lead)
            current = {"level": len(m.group(1)), "name": m.group(2), "body": []}
            sections.append(current)
        elif not line.strip():
            flush(current["body"] if current else lead)
        else:
            para.append(line.strip())
    flush(current["body"] if current else lead)

    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        f"<title>{html.escape(title)}</title>",
        "<style>.infobox{float:right}</style><script>var x=1;</script></head>",
        "<body class='mw-body'><div id='mw-content-text'>",
        f"<h1 class='article-header'>{html.escape(title)}</h1>",
        "<section data-mw-section-id='0'>",
        "<div class='hatnote navigation-not-searchable'>For other uses, see "
        f"{html.escape(title)} (disambiguation).</div>",
        "<table class='infobox'><tr><th>Material</th><td>Paper</td></tr></table>",
        "<table class='sidebar'><tr><td>Sidebar that should not appear</td></tr></table>",
        *lead,
        "</section>",
    ]
    for s in sections:
        h = f"h{s['level']}"
        parts.append(f"<details data-level='{s['level']}' open>"
                     f"<summary class='section-heading'><{h} id='x'>"
                     f"{html.escape(s['name'])}</{h}></summary><section>")
        parts += s["body"]
        parts.append("</section></details>")
    parts += [
        "<details data-level='2'><summary class='section-heading'><h2>References</h2></summary>",
        "<section><div class='reflist'><ol class='references'><li>A citation that "
        "should not appear.</li></ol></div></section></details>",
        "<details data-level='2'><summary class='section-heading'><h2>External links</h2>"
        "</summary><section><ul><li>Official site (should not appear)</li></ul>"
        "</section></details>",
        "</div></body></html>",
    ]
    return "\n".join(parts)


def math(tex: str, display: bool = False) -> str:
    """A formula exactly as Kiwix's Wikipedia ZIMs carry it (mwoffliner 1.13)."""
    kind = "display" if display else "inline"
    alt = "{\\displaystyle " + tex + "}"
    a = html.escape(alt, quote=True)
    return (f'<span class="mwe-math-element"><span class="mwe-math-mathml-{kind} mwe-math-mathml-a11y" '
            f'style="display: none;"><math xmlns="http://www.w3.org/1998/Math/MathML" alttext="{a}">'
            f'<semantics><mrow><mi>x</mi></mrow><annotation encoding="application/x-tex">{html.escape(alt)}'
            f'</annotation></semantics></math></span><img src="abc.svg" class="mwe-math-fallback-image-{kind} '
            f'mw-invert skin-invert" aria-hidden="true" alt="{a}"></span>')


MATRIX_HTML = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Matrix (mathematics)</title></head>"
    "<body><div id='mw-content-text'><h1 class='section-heading'>Matrix (mathematics)</h1>"
    "<section data-mw-section-id='0'>"
    "<p>In mathematics, a <b>matrix</b> is a rectangular array of numbers, used in "
    "<a href='Linear_algebra' title='Linear algebra'>linear algebra</a> and to pack "
    "<a href='Corrugated_cardboard' title='Corrugated cardboard'>boxes of cardboard</a>.</p>"
    "<p>The entry in the <i>i</i>th row and <i>j</i>th column of a matrix <b>A</b> is denoted "
    + math("a_{ij}") + " or " + math("A_{i,j}") + ". A pack costs $5 at "
    "<a href='./Shrubbies#History' title='The Shrubbies'>the band's</a> shows.</p>"
    "<dl><dd>" + math("\\mathbf {A} ={\\begin{bmatrix}1&2\\\\3&4\\end{bmatrix}}") + "</dd></dl>"
    "<p>See <a class='external text' href='https://example.org/'>an external site</a> and "
    "<a href='File:Matrix.svg'>this picture</a>.</p></section>"
    "<details data-level='2'><summary class='section-heading'><h2>Notation</h2></summary><section>"
    "<p>Display form:</p>" + math("\\det(\\mathbf {A} )=ad-bc", display=True) +
    "<ul><li>Square: " + math("m=n") + "</li><li>Row vector</li></ul></section></details>"
    "</div><!--htdig_noindex--><div>This article is issued from Wikipedia. Footer.</div>"
    "</body></html>")


GAMES_HTML = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'><title>List of best-selling video games</title></head>"
    "<body><div id='mw-content-text'><h1 class='section-heading'>List of best-selling video games</h1>"
    "<section data-mw-section-id='0'>"
    "<div role='note' class='hatnote navigation-not-searchable'>For best-selling consoles, see "
    "<a href='Cardboard' title='Cardboard'>Cardboard</a>.</div>"
    "<p>This is a list of the <b>best-selling video games</b> of all time. <i>Minecraft</i> "
    "leads with sales above 3&#x200b;00 million; area 5 km<sup>2</sup>, CO<sub>2</sub> 10<sup>\u22126</sup>, "
    "the 20<sup>th</sup> century, A<sub>r</sub>.<sup class='mw-ref reference'><a href='#c'>[1]</a></sup></p>"
    "<ol><li>Install it</li><li>Play it<ol><li>Mine</li><li>Craft</li></ol></li><li>Sell it</li></ol>"
    "<table class='wikitable sortable plainrowheaders'><caption>Best-selling games</caption><tbody>"
    "<tr><th>Rank</th><th>Title</th><th>Sales</th><th>Platform(s)</th></tr>"
    "<tr><td>1</td><th scope='row'><i><a href='Minecraft' title='Minecraft'>Minecraft</a></i></th>"
    "<td>300,000,000</td><td rowspan='3'>Multi-platform</td></tr>"
    "<tr><td>2</td><th scope='row'><i>Grand Theft Auto V</i></th><td>210,000,000</td></tr>"
    "<tr><td>3</td><th scope='row'><i>Tetris</i> (EA)</th><td>100,000,000<sup class='reference'>[2]</sup></td></tr>"
    "<tr><td>4</td><th scope='row'><i>Wii Sports</i></th><td>82,900,000</td><td>Wii<br>Wii U</td></tr>"
    "<tr><td colspan='4'>Sales as of 2026. <table class='periodictable'><tr><td>He</td></tr></table></td></tr>"
    "</tbody></table>"
    "<table class='navbox'><tr><td>Navigation box that should not appear</td></tr></table>"
    "</section>"
    "<details data-level='2'><summary class='section-heading'><h2>See also</h2></summary><section>"
    "<ul><li><a href='Shrubbies' title='Shrubbies'>Shrubbies</a></li></ul></section></details>"
    "<details data-level='2'><summary class='section-heading'><h2>References</h2></summary><section>"
    "<ol class='references'><li>A citation that should not appear.</li></ol></section></details>"
    "</div><!--htdig_noindex--><div>Footer.</div></body></html>")

MINECRAFT_HTML = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Minecraft</title></head>"
    "<body><div id='mw-content-text'><h1 class='section-heading'>Minecraft</h1>"
    "<section data-mw-section-id='0'>"
    "<table class='infobox ib-video-game'><tbody>"
    "<tr><th colspan='2' class='infobox-above'><i>Minecraft</i></th></tr>"
    "<tr><td colspan='2' class='infobox-image'><span></span></td></tr>"
    "<tr><th class='infobox-label'>Developer(s)</th><td class='infobox-data'>Mojang Studios</td></tr>"
    "<tr><th class='infobox-label'>Released</th><td class='infobox-data'>18 November 2011</td></tr>"
    "<tr><th colspan='2' class='infobox-header'>Sales</th></tr>"
    "<tr><td colspan='2'><table class='infobox-subbox'><tr><th>Year</th><th>Copies</th></tr>"
    "<tr><td>2014</td><td>54 million</td></tr><tr><td>2023</td><td>300 million</td></tr></table></td></tr>"
    "</tbody></table>"
    "<p><b><i>Minecraft</i></b> is a sandbox game. It costs $30.</p>"
    "<div role='note' class='hatnote navigation-not-searchable'>Main article: "
    "<a href='List_of_best-selling_video_games' title='List of best-selling video games'>"
    "List of best-selling video games</a></div>"
    "<p>It sold well.</p></section></div></body></html>")


def build(out: Path) -> Path:
    from libzim.writer import Creator, Hint, Item, StringProvider

    class Page(Item):
        def __init__(self, path, title, content, mimetype="text/html"):
            super().__init__()
            self.path, self.title, self.content, self.mime = path, title, content, mimetype

        def get_path(self):
            return self.path

        def get_title(self):
            return self.title

        def get_mimetype(self):
            return self.mime

        def get_contentprovider(self):
            return StringProvider(self.content)

        def get_hints(self):
            return {Hint.FRONT_ARTICLE: self.mime == "text/html"}

    pages = {}
    for f in sorted(FIXTURES.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        m = re.search(r"^#\s+(.+)$", text, re.M)
        title = m.group(1).strip() if m else f.stem
        pages.setdefault(title, text)          # TestShrubberies duplicates Shrubbies

    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    with Creator(str(out)).config_indexing(True, "eng") as c:
        c.set_mainpath(next(iter(pages)).replace(" ", "_"))
        for title, md in pages.items():
            c.add_item(Page(title.replace(" ", "_"), title, md_to_kiwix_html(title, md)))
        c.add_item(Page("style.css", "", "body{}", "text/css"))
        c.add_item(Page("Matrix_(mathematics)", "Matrix (mathematics)", MATRIX_HTML))
        c.add_item(Page("List_of_best-selling_video_games", "List of best-selling video games", GAMES_HTML))
        c.add_item(Page("Minecraft", "Minecraft", MINECRAFT_HTML))
        for alias, target in [("Corrugated cardboard", "Cardboard"),
                              ("The Shrubbies", "Shrubbies"),
                              ("Shrubbies (band)", "Shrubbies"),
                              ("Matrix (math)", "Matrix (mathematics)")]:
            c.add_redirection(alias.replace(" ", "_"), alias, target.replace(" ", "_"),
                              {Hint.FRONT_ARTICLE: True})
        for k, v in {"Title": "Mini Wikipedia (MOSS test)", "Language": "eng",
                     "Creator": "MOSS tests", "Publisher": "MOSS",
                     "Description": "Test fixture", "Source": "en.wikipedia.org",
                     "Name": "mini_wikipedia", "Date": "2026-10-21"}.items():
            c.add_metadata(k, v)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(FIXTURES / "mini_wikipedia.zim"))
    args = ap.parse_args()
    path = build(Path(args.out))
    print(f"wrote {path} ({path.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
