"""Rebuild tables sent over the air "disassembled" (see zim.py).

    :::table Optional caption        :::facts
    !Head\tHead\tHead                Label\tValue
    a\tb\tc                          !Section\t<
    ^\td\t<                          :::
    :::

One row per line, cells split by tabs. ^ = same cell as the one above
(rowspan), < = same cell as the one to the left (colspan), a leading ! marks
a header row. \\^ \\< \\! are the literal characters.

The web reader has the same logic in JavaScript (index.html, parseTable).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FENCE = re.compile(r"^:::(table|facts)(?: (.*))?$")


@dataclass
class Cell:
    text: str
    header: bool = False
    rowspan: int = 1
    colspan: int = 1
    hidden: bool = False          # covered by a merged cell
    origin: "Cell | None" = None  # the cell that covers it


@dataclass
class Table:
    kind: str                     # "table" or "facts"
    caption: str = ""
    rows: list[list[Cell]] = field(default_factory=list)

    @property
    def width(self) -> int:
        return max((len(r) for r in self.rows), default=0)


def parse(kind: str, caption: str, lines: list[str]) -> Table:
    t = Table(kind, caption or "")
    for line in lines:
        header = line.startswith("!")
        if header:
            line = line[1:]
        elif line.startswith("\\!"):
            line = line[1:]
        row: list[Cell] = []
        for c, raw in enumerate(line.split("\t")):
            if raw in ("^", "<"):
                if raw == "^" and t.rows and c < len(t.rows[-1]):
                    above = t.rows[-1][c]
                    origin = above.origin or above
                    if not row or (row[-1].origin or row[-1]) is not origin:
                        origin.rowspan += 1 if _first_col(t.rows[-1], c, origin) else 0
                    row.append(Cell(origin.text, origin.header, hidden=True, origin=origin))
                    continue
                if raw == "<" and row:
                    origin = row[-1].origin or row[-1]
                    if _same_row(row, origin):            # widen a cell in this row
                        origin.colspan += 1
                    row.append(Cell("", origin.header, hidden=True, origin=origin))
                    continue
            text = raw[1:] if raw in ("\\^", "\\<") else raw
            row.append(Cell(text, header))
        t.rows.append(row)
    return t


def _first_col(prev_row: list[Cell], c: int, origin: Cell) -> bool:
    """Count a rowspan once per row, at the merged cell's first column."""
    return c == 0 or (prev_row[c - 1].origin or prev_row[c - 1]) is not origin


def _same_row(row: list[Cell], origin: Cell) -> bool:
    return any(cell is origin for cell in row)


def blocks(md: str):
    """Split markdown into ('text', str) and ('table', Table) pieces."""
    lines = md.split("\n")
    i, text = 0, []
    while i < len(lines):
        m = FENCE.match(lines[i])
        if not m:
            text.append(lines[i])
            i += 1
            continue
        if text:
            yield "text", "\n".join(text)
            text = []
        j = i + 1
        body = []
        while j < len(lines) and lines[j] != ":::":
            body.append(lines[j])
            j += 1
        yield "table", parse(m.group(1), m.group(2), body)
        i = j + 1
    if text:
        yield "text", "\n".join(text)


# ---------------------------------------------------------------- output forms

def to_markdown(t: Table) -> list[str]:
    """A GitHub/Obsidian pipe table. Markdown has no merged cells, so a
    rowspan repeats its text and a colspan leaves the rest empty."""
    width = t.width
    if not width:
        return []

    def cell_text(c: Cell) -> str:
        text = c.origin.text if (c.hidden and c.origin and c.origin.colspan == 1) else (
            "" if c.hidden else c.text)
        return text.replace("|", "\\|")

    out = []
    rows = t.rows
    if t.kind == "facts" or not rows[0] or not all(c.header for c in rows[0]):
        head = ["Key facts" if t.kind == "facts" else ""] + [""] * (width - 1)
        body = rows
    else:
        head = [cell_text(c) for c in rows[0]] + [""] * (width - len(rows[0]))
        body = rows[1:]
    out.append("| " + " | ".join(head) + " |")
    out.append("|" + "---|" * width)
    for r in body:
        cells = [cell_text(c) for c in r] + [""] * (width - len(r))
        if r and all(c.header for c in r) and cells[0]:
            cells[0] = f"**{cells[0]}**"
        out.append("| " + " | ".join(cells) + " |")
    return out


def to_text(t: Table) -> list[str]:
    """Plain lines for a terminal."""
    out = [t.caption] if t.caption else []
    for r in t.rows:
        cells = [c.text for c in r if not c.hidden or (c.origin and c.origin.colspan == 1)]
        cells = [c for c in cells if c]
        if t.kind == "facts" and len(cells) == 2:
            out.append(f"{cells[0]}: {cells[1]}")
        else:
            out.append(" | ".join(cells))
    return out
