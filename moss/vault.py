"""Dress saved articles for Obsidian.

An article off the air is plain markdown. For a vault it gets:

  - YAML properties (title, source, license, fetched, tags)
  - the Wikipedia attribution as a callout, not a loose italic line
  - [[wikilinks]] to the other articles you have saved, first mention only,
    so Obsidian's graph and backlinks work

Links are added here, on your machine, from titles you already hold. Nothing
extra travels over the radio.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from .cache import Cache

# "text under CC BY-SA 4.0." -- the sentence ends at a period followed by
# whitespace or the closing '*', not at the dot inside "4.0".
SOURCE_LINE = re.compile(
    r"^\*Source:\s*Wikipedia,\s*(\S+?)\s*[—-]+\s*text under (.+?)\.(?=\s|\*)", re.M)
BAD_FILENAME = re.compile(r'[\\/:*?"<>|#^\[\]]')
MIN_LINK_TITLE = 4


def note_filename(title: str) -> str:
    """Obsidian resolves [[Title]] by file name, so the name must be the title."""
    name = BAD_FILENAME.sub(" ", title)
    return " ".join(name.split()).strip(". ") or "Untitled"


def _yaml(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


PROTECTED = re.compile(r"\[\[[^\[\]]+\]\]|(?<!\\)\$\$?(?!\s)(?:\\.|[^\\$])+?(?<!\s)\$\$?")
WIKILINK = re.compile(r"\[\[([^\[\]|]+)(\|[^\[\]]*)?\]\]")


def _fix_link_targets(line: str) -> str:
    """[[AC/DC]] must point at the note file 'AC DC', which is how
    note_filename() saves it; keep the words the reader sees."""
    def fix(m):
        target, rest = m.group(1), m.group(2)
        name = note_filename(target)
        if name == target:
            return m.group(0)
        return f"[[{name}{rest or '|' + target}]]"
    return WIKILINK.sub(fix, line)


def _link_first_mentions(lines: list[str], titles: list[str]) -> list[str]:
    """Link each title once, in running text only (never headings, callouts,
    code, or text already inside [[ ]])."""
    done: set[str] = set()
    out = []
    in_code = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_code = not in_code
        if in_code or line.startswith(("#", ">", "    ")) or not line.strip():
            out.append(line)
            continue
        line = _fix_link_targets(line)
        for title in titles:                      # longest first
            if title in done:
                continue
            pattern = re.compile(r"(?<![\w\[|])" + re.escape(title) + r"(?![\w\]|])")
            link = (f"[[{note_filename(title)}]]" if note_filename(title) == title
                    else f"[[{note_filename(title)}|{title}]]")
            # Only look in plain text: not inside links or formulas.
            pieces, pos, hit = [], 0, False
            for m in list(PROTECTED.finditer(line)) + [None]:
                end = m.start() if m else len(line)
                chunk = line[pos:end]
                if not hit:
                    chunk, n = pattern.subn(lambda _m: link, chunk, count=1)
                    hit = bool(n)
                pieces.append(chunk)
                if m:
                    pieces.append(m.group(0))
                    pos = m.end()
            if hit:
                line = "".join(pieces)
                done.add(title)
        out.append(line)
    return out


def _tables_for_obsidian(lines: list[str]) -> list[str]:
    """Rebuild the tables sent over the air as markdown tables; the infobox
    becomes a collapsible 'Key facts' callout."""
    from . import tables
    out: list[str] = []
    for kind, piece in tables.blocks("\n".join(lines)):
        if kind == "text":
            out.extend(piece.split("\n"))
            continue
        rows = tables.to_markdown(piece)
        if piece.kind == "facts":
            out += ["> [!abstract]- Key facts"] + [f"> {r}" for r in rows]
        else:
            if piece.caption:
                out += [f"**{piece.caption}**", ""]
            out += rows
    return out


def to_obsidian(title: str, text: str, fetched_at: float | None = None,
                known_titles: list[str] | None = None) -> str:
    """Turn one received article into an Obsidian note."""
    body = text.strip().splitlines()

    # The note's title is its file name; drop our leading '# Title'.
    while body and not body[0].strip():
        body.pop(0)
    if body and re.match(r"^#\s+", body[0]):
        body.pop(0)

    source_url = license_ = ""
    if m := SOURCE_LINE.search("\n".join(body[:6])):
        source_url, license_ = m.group(1), m.group(2).strip()
        body = [ln for ln in body if not ln.startswith("*Source:")]

    while body and not body[0].strip():
        body.pop(0)

    links = sorted({t for t in (known_titles or [])
                    if t != title and len(t) >= MIN_LINK_TITLE},
                   key=lambda t: (-len(t), t))
    body = _link_first_mentions(body, links)
    body = _tables_for_obsidian(body)

    props = ["---", f"title: {_yaml(title)}"]
    if source_url:
        props.append(f"source: {source_url}")
    if license_:
        props.append(f"license: {_yaml(license_)}")
    if fetched_at:
        props.append(f"fetched: {time.strftime('%Y-%m-%d', time.localtime(fetched_at))}")
    props += ["via: MOSS (mesh radio)", "tags:"]
    if source_url:
        props.append("  - wikipedia")
    props += ["  - moss", "---", ""]

    callout = []
    if source_url:
        callout = [f"> [!info] From Wikipedia, received over radio",
                   f"> [Original article]({source_url}). Text under {license_ or 'its original license'}.",
                   ""]
    return "\n".join(props + callout + body).rstrip() + "\n"


def export_vault(cache: Cache, vault: Path, folder: str = "Wikipedia",
                 extra_titles: list[str] | None = None) -> list[Path]:
    """Write every cached article into <vault>/<folder>/ as an Obsidian note.

    Safe to re-run: a note is rewritten from the cache each time. Files that
    aren't ours are never touched (only <title>.md names from the cache).
    """
    items = cache.list_articles()
    known = sorted({a.title for a in items} | set(extra_titles or []))
    target = vault / folder
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for a in items:
        hit = cache.get_article(a.title)
        if not hit:
            continue
        note = to_obsidian(a.title, hit[1], a.fetched_at, known)
        path = target / f"{note_filename(a.title)}.md"
        path.write_text(note, encoding="utf-8")
        written.append(path)
    return written
