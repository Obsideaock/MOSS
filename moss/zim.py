"""Serve articles from a Kiwix .zim file, fully offline.

    source = ZimSource("/media/moss/KIWIX/wikipedia_en_all_nopic_2026-06.zim")
    results = await source.search("shrubbies band")
    title, text = await source.article(results[0].title)

This is what lets the Pi answer requests with no internet at all. It reads
the ZIM in place (nothing is unpacked; the 57 GB file is opened, not loaded)
and turns each article's HTML into the same markdown the Wikipedia source
produces, so the client, the cache, the web reader and the Obsidian export
can't tell the difference.

Needs `pip install libzim` (wheels exist for Windows, macOS, x86-64 Linux
and the 64-bit Raspberry Pi OS).
"""

from __future__ import annotations

import asyncio
import re
import threading
import urllib.parse
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable  # noqa: F401  (used in a type comment)

from .protocol import SearchResult
from .sources import SourceError, prepare_article

# Sections that are mostly links and citations, not reading material.
# ("See also" is kept: with links, it's Wikipedia's own reading list.)
DROP_SECTIONS = {
    "references", "external links", "further reading", "notes",
    "bibliography", "sources", "citations", "footnotes", "works cited",
    "notes and references", "references and notes", "explanatory notes",
}

# Elements whose whole contents are skipped: figures, media, footnote markers,
# edit links, navigation boxes, etc. Tables are decided separately.
SKIP_TAGS = {"script", "style", "noscript", "figure", "figcaption",
             "audio", "video", "svg", "img", "button", "nav",
             "head", "title", "object", "iframe", "map", "template"}
SKIP_CLASSES = {"reference", "mw-ref", "reflist", "references", "navbox",
                "mw-editsection", "noprint", "metadata", "thumb",
                "sidebar", "ambox", "mbox-small", "mw-empty-elt",
                "shortdescription", "reference-text", "toc", "printfooter",
                "gallery", "authority-control", "portalbox", "vertical-navbox",
                "navbox-inner", "periodictable", "tmbox", "ombox", "cmbox",
                "fmbox", "imbox", "toccolours", "sistersitebox", "side-box"}
HATNOTE_CLASSES = {"hatnote", "dablink", "rellink"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "source", "track", "wbr", "param"}
BLOCK = {"p", "div", "section", "details", "summary", "ul", "ol", "dl",
         "blockquote", "pre", "body", "article", "main", "header", "footer"}
INVISIBLE = re.compile("[\u200b\u200c\u200d\u00ad\ufeff\u2060]")
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
FORMAT = {"b": "**", "strong": "**", "i": "*", "em": "*"}

LANG_CODES = {"eng": "en", "fra": "fr", "deu": "de", "spa": "es", "ita": "it",
              "por": "pt", "nld": "nl", "rus": "ru", "pol": "pl", "jpn": "ja",
              "zho": "zh", "ara": "ar", "swe": "sv", "ukr": "uk", "vie": "vi"}

_SUP_FROM, _SUP_TO = "0123456789+-−=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ⁿⁱ"
_SUB_FROM, _SUB_TO = "0123456789+-\u2212=()aeoxhklmnpstijruv", "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜᵢⱼᵣᵤᵥ"
assert len(_SUP_FROM) == len(_SUP_TO) and len(_SUB_FROM) == len(_SUB_TO)
SUP, SUB = str.maketrans(_SUP_FROM, _SUP_TO), str.maketrans(_SUB_FROM, _SUB_TO)


def script_text(text: str, kind: str) -> str:
    """10<sup>6</sup> -> 10⁶ and CO<sub>2</sub> -> CO₂, instead of '106'
    and 'CO2'. Anything without a Unicode form is written x^(...) / x_(...);
    ordinal endings (20<sup>th</sup>) are just letters."""
    t = text.strip()
    if not t or "[[" in t or "$" in t:
        return text
    chars, table = (_SUP_FROM, SUP) if kind == "sup" else (_SUB_FROM, SUB)
    if all(c in chars for c in t):
        return t.translate(table)
    if kind == "sup" and t.isalpha() and len(t) <= 3:
        return t
    return f"^({t})" if kind == "sup" else f"_({t})"


# ---------------------------------------------------------------- tables on the wire
#
# Tables go over the air "disassembled": one line per row, cells separated by
# tabs, nothing repeated. A cell merged with the one above is sent as ^, one
# merged with the cell to its left as <, and a header row starts with !.
#
#     :::table Best-selling games
#     !Rank\tTitle\tSales\tPlatform
#     1\t[[Minecraft]]\t300,000,000\tMulti-platform
#     2\t[[Grand Theft Auto V]]\t210,000,000\t^
#     :::
#
# An infobox is the same thing under :::facts (label \t value). The reader
# rebuilds real tables from this, and `moss export` turns it into markdown
# tables for Obsidian. See tables.py.

def _cell(text: str) -> str:
    text = text.replace("\t", " ").strip()
    if text in ("^", "<"):
        return "\\" + text
    return text


def _int(v, default=1, cap=60) -> int:
    try:
        return max(1, min(cap, int(str(v).strip().split()[0])))
    except (ValueError, IndexError, TypeError):
        return default


NON_ARTICLE = {"file", "image", "media", "special", "help", "wikipedia", "template",
               "category", "portal", "talk", "user", "draft", "module", "mediawiki",
               "book", "timedtext", "wp", "wiktionary", "wikt", "s", "commons"}


def link_path(href: str) -> str | None:
    """A ZIM-relative article path from an <a href>, or None for anything
    that isn't a link to another article (web links, anchors, files...)."""
    if not href or "://" in href or href.startswith(("#", "mailto:", "//", "?")):
        return None
    path = href.split("#", 1)[0].split("?", 1)[0]
    while path.startswith(("./", "../")):
        path = path[2:] if path.startswith("./") else path[3:]
    if path.startswith("A/"):                     # old ZIM namespace scheme
        path = path[2:]
    path = urllib.parse.unquote(path)
    if not path:
        return None
    if "." in path[-6:] and path.rsplit(".", 1)[-1].lower() in {
            "css", "js", "png", "jpg", "jpeg", "svg", "gif", "webp", "pdf", "ogg", "webm"}:
        return None
    if ":" in path and path.split(":", 1)[0].strip().lower() in NON_ARTICLE:
        return None
    return path


def default_resolver(path: str, title: str | None) -> str | None:
    return title or path.replace("_", " ")


def clean_tex(tex: str) -> str:
    tex = " ".join(tex.split())
    m = re.fullmatch(r"\{\\(?:displaystyle|textstyle)\s*(.*)\}", tex, re.S)
    if m:
        tex = m.group(1).strip()
    return tex.replace("$", "\\$")


class _Converter(HTMLParser):
    """The reading text: headings, paragraphs, lists, [[links]], $formulas$,
    *italic* / **bold**, ¹²³ scripts, hatnotes, infoboxes and data tables."""

    def __init__(self, resolve=default_resolver, links: bool = True):
        super().__init__(convert_charrefs=True)
        # (tag, kind): kind is None or one of skip / math / link / fmt /
        # script / note / table / layout / caption / row / cell
        self.stack: list[tuple[str, str | None]] = []
        self.skip = 0
        self.blocks: list[str] = []
        self.buf: list[str] = []
        self.flushes = 0
        self.heading: int | None = None
        self.page_title = ""
        self.drop_level: int | None = None         # dropping a References-like section
        self.lists: list[list] = []                # [["ul"|"ol", counter], ...]
        self.in_li = False
        self.in_pre = False
        self.note = 0                              # inside a hatnote
        self.fmt = 0                               # inside bold/italic already
        self.resolve, self.want_links = resolve, links
        self.marks: list[tuple] = []               # (flush no., buf index, kind, data)
        self.math: dict | None = None              # the formula being read
        self.tables: list[dict] = []               # data tables / infoboxes being read
        self.cell: dict | None = None              # the table cell being read
        self.inner = 0                             # tables flattened into a cell

    # ------------------------------------------------------------ text blocks
    def _flush(self) -> None:
        text = "".join(self.buf)
        self.buf = []
        self.flushes += 1
        if not self.in_pre:
            text = re.sub(r"[ \t\r\n]+", " ", text).strip()
        if not text:
            return
        if self.cell is not None:                  # inside a table: part of the cell
            self.cell["parts"].append(text)
            return
        if self.drop_level is not None:
            return
        # A formula standing alone (Wikipedia's indented ":<math>") is display math.
        m = re.fullmatch(r"\$([^$]+)\$", text)
        if m and not self.in_li:
            text = f"$${m.group(1)}$$"
        if self.note:
            text = f"*{text}*"
        if self.in_li and self.lists:
            kind, n = self.lists[-1]
            bullet = f"{n}." if kind == "ol" else "-"
            text = "  " * (len(self.lists) - 1) + f"{bullet} {text}"
            self.in_li = False                     # a second paragraph isn't a new item
        self.blocks.append(text)

    def _skips(self, tag: str, a: dict) -> bool:
        if tag in SKIP_TAGS:
            return True
        if a.get("role") == "navigation" or a.get("aria-hidden") == "true":
            return True
        style = (a.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or "color:transparent" in style:
            return True
        classes = set((a.get("class") or "").split())
        return bool(classes & SKIP_CLASSES)

    # ------------------------------------------------------------ formulas
    def _math_start(self, tag: str, a: dict) -> None:
        """Inside a formula: pick the LaTeX source out of whichever of
        alttext / annotation / fallback image carries it."""
        m = self.math
        if tag == "math":
            if a.get("alttext") and not m["tex"]:
                m["tex"] = a["alttext"]
            if a.get("display") == "block":
                m["display"] = True
        elif tag == "annotation" and "tex" in (a.get("encoding") or ""):
            m["cap"] = []
        elif tag == "img" and "mwe-math-fallback" in (a.get("class") or ""):
            if a.get("alt") and not m["tex"]:
                m["tex"] = a["alt"]
            if "display" in a.get("class", ""):
                m["display"] = True

    def _math_end(self) -> None:
        m, self.math = self.math, None
        tex = clean_tex(m["tex"] or "".join(m["cap"] or []))
        if not tex or self.skip:
            return
        if m["display"] and self.cell is None:
            self._flush()
            if self.drop_level is None:
                self.blocks.append(f"$${tex}$$")
        else:
            self.buf.append(f"${tex}$")

    # ------------------------------------------------------------ tables
    def _table_start(self, tag: str, a: dict) -> None:
        classes = set((a.get("class") or "").split())
        if self._skips(tag, a):
            self.stack.append((tag, "skip"))
            self.skip += 1
            return
        if self.tables and self.tables[-1]["kind"] in ("facts", "merge"):
            # Infoboxes nest tables (election candidates, chemistry data):
            # their rows become rows of the infobox.
            outer = self.tables[-1]
            if self.cell is not None:
                self._cell_end()
            if outer["row"] is not None:
                self._row_end()
            self.tables.append({"kind": "merge", "rows": [], "row": None, "row_th": True,
                                "col": 0, "pending": {}, "caption": "", "parent": outer})
            self.stack.append((tag, "table"))
            return
        if self.tables:
            # A table inside a data-table cell: read it as plain text in the cell.
            self.inner += 1
            self.stack.append((tag, "inner"))
            return
        if any(c == "infobox" or (c.startswith("infobox") and c != "infobox-subbox")
               for c in classes):
            kind = "facts"
        elif "wikitable" in classes:
            kind = "table"
        else:
            # A table used for layout: let its contents flow as ordinary text.
            self.stack.append((tag, "layout"))
            return
        self._flush()
        self.tables.append({"kind": kind, "rows": [], "row": None, "row_th": True,
                            "col": 0, "pending": {}, "caption": ""})
        self.stack.append((tag, "table"))

    def _place_pending(self, t: dict, upto: int | None = None) -> None:
        """Fill columns still covered by a rowspan from an earlier row."""
        while t["col"] in t["pending"] and (upto is None or t["col"] < upto):
            t["row"].append("^")
            t["pending"][t["col"]] -= 1
            if not t["pending"][t["col"]]:
                del t["pending"][t["col"]]
            t["col"] += 1

    def _cell_end(self) -> None:
        self._flush()
        cell, self.cell = self.cell, None
        t = self.tables[-1]
        if t["row"] is None:
            return
        text = " · ".join(cell["parts"])
        self._place_pending(t)
        t["row"].append(_cell(text))
        if not cell["th"]:
            t["row_th"] = False
        start = t["col"]
        for extra in range(1, cell["colspan"]):
            t["row"].append("<")
        t["col"] += cell["colspan"]
        if cell["rowspan"] > 1:
            for c in range(start, start + cell["colspan"]):
                t["pending"][c] = cell["rowspan"] - 1

    def _row_end(self) -> None:
        t = self.tables[-1]
        row = t["row"]
        if row is None:
            return
        # Columns at the end of the row still covered from above.
        while any(c >= t["col"] for c in t["pending"]):
            if t["col"] in t["pending"]:
                self._place_pending(t)
            else:
                row.append("")
                t["col"] += 1
        t["row"] = None
        if not any(c not in ("", "^", "<") for c in row):
            return                                 # an image-only or empty row
        if (t["kind"] == "facts" and len([c for c in row if c not in ("<", "")]) == 1
                and row[0].strip("*").lower() == self.page_title.lower()):
            return                                 # the infobox repeating the title
        line = "\t".join(row)
        if t["row_th"]:
            line = "!" + line
        elif line.startswith("!"):
            line = "\\" + line
        t["rows"].append(line)

    def _table_end(self) -> None:
        if self.cell is not None:
            self._cell_end()
        if self.tables and self.tables[-1]["row"] is not None:
            self._row_end()
        t = self.tables.pop()
        if t["kind"] == "merge":
            parent = t["parent"]
            parent["rows"].extend(t["rows"])
            # Whatever follows the nested table in the same infobox cell
            # ("This lists parties that won seats") becomes a row of its own.
            parent["row"], parent["row_th"], parent["col"] = [], False, 0
            self.cell = {"parts": [], "th": False, "rowspan": 1, "colspan": 1}
            return
        if not t["rows"] or self.drop_level is not None:
            return
        head = ":::facts" if t["kind"] == "facts" else (
            ":::table" + (" " + t["caption"] if t["caption"] else ""))
        self.blocks.append("\n".join([head, *t["rows"], ":::"]))

    def _in_table_part(self, tag: str, a: dict) -> bool:
        """tr / th / td / caption of the data table being read."""
        if not self.tables or self.inner:
            return False
        t = self.tables[-1]
        if tag == "caption":
            self._flush()
            self.cell = {"parts": [], "th": True, "caption": True, "rowspan": 1, "colspan": 1}
            self.stack.append((tag, "caption"))
            return True
        if tag == "tr":
            if self.cell is not None:
                self._cell_end()
            if t["row"] is not None:
                self._row_end()
            t["row"], t["row_th"], t["col"] = [], True, 0
            self.stack.append((tag, "row"))
            return True
        if tag in ("th", "td"):
            if self.cell is not None:              # unclosed previous cell
                self._cell_end()
            if t["row"] is None:
                t["row"], t["row_th"], t["col"] = [], True, 0
            self._flush()
            self.cell = {"parts": [], "th": tag == "th", "rowspan": _int(a.get("rowspan")),
                         "colspan": _int(a.get("colspan"))}
            self.stack.append((tag, "cell"))
            return True
        return False

    # ------------------------------------------------------------ parser callbacks
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.math is not None:
            self._math_start(tag, a)
            if tag not in VOID:
                self.stack.append((tag, None))
            return
        if tag in VOID:
            if tag == "br" and not self.skip:
                if self.cell is not None:
                    self._flush()                  # a line break in a cell: "Wii · Wii U"
                else:
                    self.buf.append("\n" if self.in_pre else " ")
            return
        classes = (a.get("class") or "").split()
        if not self.skip and (tag == "math" or "mwe-math-element" in classes):
            self.math = {"tex": None, "display": False, "cap": None}
            self.stack.append((tag, "math"))
            self._math_start(tag, a)
            return
        if self.skip:
            self.stack.append((tag, "skip") if self._skips(tag, a) else (tag, None))
            if self.stack[-1][1] == "skip":
                self.skip += 1
            return
        if tag == "table":
            self._table_start(tag, a)
            return
        if self._skips(tag, a):
            self.stack.append((tag, "skip"))
            self.skip += 1
            return
        if tag in ("tr", "th", "td", "caption") and self._in_table_part(tag, a):
            return
        if tag == "a" and self.want_links:
            path = link_path(a.get("href", ""))
            target = self.resolve(path, a.get("title")) if path else None
            self.marks.append((self.flushes, len(self.buf), "link", target))
            self.stack.append((tag, "link"))
            return
        if tag in FORMAT:
            if self.fmt or self.note or self.heading is not None:
                self.stack.append((tag, None))     # no nested or heading markers
            else:
                self.fmt += 1
                self.marks.append((self.flushes, len(self.buf), "fmt", FORMAT[tag]))
                self.stack.append((tag, "fmt"))
            return
        if tag in ("sup", "sub"):
            self.marks.append((self.flushes, len(self.buf), "script", tag))
            self.stack.append((tag, "script"))
            return
        if tag == "div" and set(classes) & HATNOTE_CLASSES and self.cell is None:
            self._flush()
            self.note += 1
            self.stack.append((tag, "note"))
            return
        self.stack.append((tag, None))
        if self.cell is not None:                  # inside a table cell: no headings/lists
            if tag in HEADINGS or tag in BLOCK or tag in ("li", "dd", "dt", "td", "th", "tr"):
                self._flush()
            return
        if tag in HEADINGS or tag in BLOCK or tag in ("li", "dd", "dt", "td", "th", "tr"):
            self._flush()
        if tag in HEADINGS:
            self.heading = HEADINGS[tag]
        elif tag in ("ul", "ol"):
            self.lists.append([tag, 0])
        elif tag == "li":
            if self.lists:
                self.lists[-1][1] += 1
            self.in_li = True
        elif tag == "pre":
            self.in_pre = True

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        # Pop to the matching open tag; HTML in the wild isn't always tidy.
        if not any(t == tag for t, _ in self.stack):
            return
        while self.stack:
            t, kind = self.stack.pop()
            if kind == "math":
                self._math_end()
            elif self.math is not None:
                if t == "annotation" and self.math.get("cap") is not None and not self.math["tex"]:
                    self.math["tex"] = "".join(self.math["cap"])
            elif kind == "skip":
                self.skip -= 1
            elif kind in ("link", "fmt", "script"):
                self._mark_end(kind)
            elif kind == "note":
                self._flush()
                self.note -= 1
            elif kind == "table":
                self._table_end()
            elif kind == "caption":
                self._flush()
                cell, self.cell = self.cell, None
                if self.tables and cell:
                    self.tables[-1]["caption"] = " · ".join(cell["parts"]).replace("\n", " ")
            elif kind == "row":
                if self.cell is not None:
                    self._cell_end()
                if self.tables:
                    self._row_end()
            elif kind == "cell":
                if self.cell is not None:
                    self._cell_end()
            elif kind == "layout":
                self._flush()
            elif kind == "inner":
                self._flush()
                self.inner -= 1
            elif not self.skip:
                self._close(t)
            if t == tag:
                break

    def _mark_end(self, kind: str) -> None:
        flushes, start, k, data = self.marks.pop()
        if kind == "fmt":
            self.fmt -= 1
        if flushes != self.flushes or start > len(self.buf):
            return                       # crossed a block boundary: leave the words as they are
        raw = "".join(self.buf[start:])
        lead = " " if raw[:1].isspace() else ""
        trail = " " if raw[-1:].isspace() else ""
        text = re.sub(r"\s+", " ", raw).strip()
        if not text:
            return
        if kind == "script":
            self.buf[start:] = [lead + script_text(text, data) + trail]
            return
        if kind == "fmt":
            if "**" in text or text.startswith("*") or text.endswith("*"):
                return
            self.buf[start:] = [lead + data + text + data + trail]
            return
        target = data                    # a link
        if not target:
            return
        words = re.sub(r"(?<!\\)\*+", "", text).strip()   # no bold/italic inside [[ ]]
        if not words or "[[" in words or "]]" in words:
            return
        target = target.replace("|", "/").replace("[", "(").replace("]", ")")
        if words.lower() == target.lower():
            link = f"[[{words}]]"
        else:
            link = f"[[{target}|{words.replace('|', '/')}]]"
        self.buf[start:] = [lead + link + trail]

    def _close(self, tag: str) -> None:
        if tag in HEADINGS and self.heading is not None:
            name = re.sub(r"\s+", " ", "".join(self.buf)).strip()
            name = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", name)   # no links in headings
            name = re.sub(r"(?<!\\)\*+", "", name).replace("\\*", "*").strip()
            self.buf = []
            level = self.heading
            self.heading = None
            if level == 1:
                self.page_title = name
            if self.drop_level is not None and level <= self.drop_level:
                self.drop_level = None
            if name.lower() in DROP_SECTIONS:
                self.drop_level = level
            elif name and self.drop_level is None and level > 1:
                self.blocks.append("#" * level + " " + name)
            return
        if tag in BLOCK or tag in ("li", "dd", "dt"):
            self._flush()
        if tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self.in_li = False
        elif tag == "li":
            self.in_li = False
        elif tag == "pre":
            self.in_pre = False

    def handle_data(self, data):
        if self.math is not None:
            if self.math.get("cap") is not None:
                self.math["cap"].append(data)
            return
        if not self.skip:
            # A literal $ or * must not be read as a formula or emphasis.
            data = INVISIBLE.sub("", data)   # zero-width spaces, soft hyphens
            self.buf.append(data.replace("$", "\\$").replace("*", "\\*"))

    def result(self) -> str:
        while self.tables:
            self._table_end()
        self._flush()
        return "\n\n".join(self.blocks)


def html_to_markdown(html: str, resolve=default_resolver, links: bool = True) -> str:
    """Article HTML -> markdown with [[links]], $math$ and :::tables.

    resolve(path, title) turns a link into the target article's title, or
    None to leave the words unlinked (e.g. the article isn't in the library).
    """
    # Kiwix appends a licence footer after this marker; the source line we
    # add already says the same thing in fewer bytes.
    html = html.split("<!--htdig_noindex-->", 1)[0]
    conv = _Converter(resolve, links)
    conv.feed(html)
    conv.close()
    text = conv.result()
    text = re.sub(r"\[(?:\d+|[a-z]|citation needed|note \d+)\]", "", text)
    text = re.sub(r"[ \t]+([,.;:])", r"\1", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


P_TAG = re.compile(r"<p[\s>].*?</p>", re.S | re.I)
TAGS = re.compile(r"<[^>]+>")


def lead_blurb(html: str, limit: int = 90) -> str:
    """First real sentence of the article, short enough for a result list."""
    for m in P_TAG.finditer(html[:60000]):
        para = m.group(0)
        para = re.sub(r"<sup.*?</sup>", "", para, flags=re.S | re.I)
        para = re.sub(r"<style.*?</style>", "", para, flags=re.S | re.I)
        para = re.sub(r"<math.*?</math>", "", para, flags=re.S | re.I)
        text = re.sub(r"\s+", " ", unescape(TAGS.sub("", para))).strip()
        text = re.sub(r"\[\d+\]", "", text)
        if len(text) < 30:
            continue
        sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
        if len(sentence) > limit:
            sentence = sentence[:limit - 1].rsplit(" ", 1)[0] + "…"
        return sentence
    return ""


# ---------------------------------------------------------------- the source

class ZimSource:
    def __init__(self, path: str | Path, max_bytes: int | None = None,
                 results: int = 8, links: bool = True):
        try:
            from libzim.reader import Archive
        except ImportError as exc:
            raise SystemExit("reading a .zim needs libzim: pip install libzim") from exc
        self.path = Path(path)
        if not self.path.is_file():
            raise SystemExit(f"no such file: {self.path}")
        self.zim = Archive(str(self.path))
        self.max_bytes, self.results, self.links = max_bytes, results, links
        self._lock = threading.Lock()       # one lookup at a time; the Pi is small
        self.site = self._site()

    # ------------------------------------------------------------ facts about the file

    def _meta(self, key: str) -> str:
        try:
            return bytes(self.zim.get_metadata(key)).decode("utf-8", "replace").strip()
        except Exception:
            return ""

    def _site(self) -> str:
        source = self._meta("Source")
        if source:
            host = urllib.parse.urlparse(source if "://" in source
                                         else "https://" + source).netloc
            if host:
                return host
        lang = self._meta("Language").split(",")[0].strip()
        return f"{LANG_CODES.get(lang, lang[:2] or 'en')}.wikipedia.org"

    def describe(self) -> str:
        name = self._meta("Title") or self.path.name
        idx = []
        if self.zim.has_fulltext_index:
            idx.append("full-text search")
        if self.zim.has_title_index:
            idx.append("title search")
        return (f"{name}: {self.zim.article_count:,} articles, "
                f"{self.path.stat().st_size / 1e9:.1f} GB, "
                f"{' + '.join(idx) or 'NO search index'}")

    # ------------------------------------------------------------ lookups

    def _resolve(self, entry):
        for _ in range(8):                  # redirect chains are short; loops aren't
            if not entry.is_redirect:
                return entry
            entry = entry.get_redirect_entry()
        return entry

    def _html(self, entry) -> str | None:
        item = entry.get_item()
        if not item.mimetype.startswith("text/html"):
            return None
        return bytes(item.content).decode("utf-8", "replace")

    def _find(self, title: str):
        """Exact title, then path spellings, then a case-insensitive title match."""
        z = self.zim
        title = title.strip()
        candidates = [title, title[:1].upper() + title[1:]]
        for t in candidates:
            try:
                if z.has_entry_by_title(t):
                    return self._resolve(z.get_entry_by_title(t))
            except Exception:
                pass
        for t in candidates:
            for path in (t.replace(" ", "_"), "A/" + t.replace(" ", "_"), t):
                try:
                    if z.has_entry_by_path(path):
                        return self._resolve(z.get_entry_by_path(path))
                except Exception:
                    pass
        for path in self._suggest(title, 10):
            raw = z.get_entry_by_path(path)
            if raw.title.lower() == title.lower():
                return self._resolve(raw)
        return None

    def _suggest(self, query: str, n: int) -> list[str]:
        if not self.zim.has_title_index:
            return []
        from libzim.suggestion import SuggestionSearcher
        try:
            s = SuggestionSearcher(self.zim).suggest(query)
            return list(s.getResults(0, n))
        except Exception:
            return []

    def _fulltext(self, query: str, n: int) -> list[str]:
        if not self.zim.has_fulltext_index:
            return []
        from libzim.search import Query, Searcher
        try:
            s = Searcher(self.zim).search(Query().set_query(query))
            return list(s.getResults(0, n))
        except Exception:
            return []

    # ------------------------------------------------------------ the Source interface

    def _search(self, query: str) -> list[SearchResult]:
        with self._lock:
            n = self.results
            exact = self._find(query)
            titles = self._suggest(query, n)
            full = self._fulltext(query, n * 2)
            # Exact title first, then alternate: best full-text hit, best
            # title-prefix hit, ... so both "shrubbies band" and "boulder co"
            # find what people mean.
            ordered: list = [exact] if exact else []
            for i in range(max(len(full), len(titles))):
                for lst in (full, titles):
                    if i < len(lst):
                        ordered.append(lst[i])
            out, seen = [], set()
            for item in ordered:
                try:
                    entry = item if not isinstance(item, str) else \
                        self._resolve(self.zim.get_entry_by_path(item))
                    if entry.path in seen:
                        continue
                    html = self._html(entry)
                except Exception:
                    continue
                if html is None:
                    continue
                seen.add(entry.path)
                out.append(SearchResult(entry.title, lead_blurb(html)))
                if len(out) >= n:
                    break
            return out

    def _link_target(self, path: str, title: str | None, seen: dict) -> str | None:
        """The real title of a linked article, following redirects; None if
        the library doesn't have it (so the words stay plain text)."""
        if path in seen:
            return seen[path]
        target = None
        for p in (path, "A/" + path):
            try:
                if self.zim.has_entry_by_path(p):
                    entry = self._resolve(self.zim.get_entry_by_path(p))
                    if entry.get_item().mimetype.startswith("text/html"):
                        target = entry.title
                    break
            except Exception:
                pass
        seen[path] = target
        return target

    def _article(self, title: str) -> tuple[str, str]:
        with self._lock:
            entry = self._find(title)
            if entry is None:
                raise SourceError(f"no such article: {title[:60]}")
            html = self._html(entry)
            if html is None:
                raise SourceError("not an article")
            real = entry.title
            path = entry.path
            seen: dict = {}
            body = html_to_markdown(
                html, lambda p, t: self._link_target(p, t, seen), self.links)
        if not body.strip():
            raise SourceError("article is empty")
        page = path[2:] if path.startswith("A/") else path
        url = f"https://{self.site}/wiki/{urllib.parse.quote(page)}"
        md = (f"# {real}\n\n"
              f"*Source: Wikipedia, {url} — text under CC BY-SA 4.0. "
              f"Images and navigation removed.*\n\n{body}\n")
        return real, prepare_article(md, self.max_bytes)

    async def search(self, query: str) -> list[SearchResult]:
        return await asyncio.to_thread(self._search, query)

    async def article(self, title: str) -> tuple[str, str]:
        return await asyncio.to_thread(self._article, title)
