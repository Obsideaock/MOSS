"""Compare two files: are they identical, how similar, and what would an
update between them cost to send over the mesh?

    python compare_files.py sample_page.md got.md
    python compare_files.py v1.md v2.md --show-diff

Answers three questions:

  1. Identical?  Byte-for-byte, by hash. This is the check after a transfer.
  2. How similar? Percentage, plus which lines changed.
  3. What would it cost? Bytes and datagrams to turn the first file into the
     second, by each method on the compression ladder. This is the number
     that matters for MOSS: both ends already hold version N, so sending
     version N+1 should cost far less than sending the whole page.

zstandard is optional (pip install zstandard); without it the dictionary
methods are skipped and everything else still runs.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import math
import sys
import zlib
from pathlib import Path

try:
    import zstandard as zstd
except ImportError:
    zstd = None

MAX_DATAGRAM = 163
MOSS_HEADER = 5
USABLE = MAX_DATAGRAM - MOSS_HEADER


def packets(n: int) -> int:
    return math.ceil(n / USABLE) if n else 0


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def unified_delta(old: str, new: str) -> bytes:
    """Smallest plain-text delta: unified diff with no context lines."""
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        n=0, lineterm="\n")).encode()


def zstd_with_dict(data: bytes, dictionary: bytes, level: int = 19) -> bytes:
    d = zstd.ZstdCompressionDict(dictionary, dict_type=zstd.DICT_TYPE_RAWCONTENT)
    return zstd.ZstdCompressor(level=level, dict_data=d).compress(data)


def report_identity(a: bytes, b: bytes, name_a: str, name_b: str) -> bool:
    same = a == b
    print(f"{name_a}: {len(a):7d} bytes   sha256 {digest(a)}")
    print(f"{name_b}: {len(b):7d} bytes   sha256 {digest(b)}")
    print()
    if same:
        print("IDENTICAL — byte for byte, hashes match")
    else:
        print(f"DIFFERENT — {len(b) - len(a):+d} bytes")
    return same


def report_similarity(old: str, new: str, show_diff: bool) -> None:
    old_lines = old.splitlines()
    new_lines = new.splitlines()

    ratio = difflib.SequenceMatcher(None, old, new).ratio()
    line_ratio = difflib.SequenceMatcher(None, old_lines, new_lines).ratio()
    print(f"\nsimilarity: {ratio:.1%} of characters, {line_ratio:.1%} of lines")

    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    added = removed = changed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "insert":
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "replace":
            changed += max(i2 - i1, j2 - j1)
    print(f"lines: {len(old_lines)} -> {len(new_lines)}   "
          f"{added} added, {removed} removed, {changed} changed")

    if show_diff:
        print("\n--- changes ---")
        for line in difflib.unified_diff(old_lines, new_lines,
                                         "first", "second", lineterm="", n=1):
            print(line)
    elif added or removed or changed:
        print("(pass --show-diff to see them)")


def report_cost(old: bytes, new: bytes) -> None:
    """What it would take to turn the first file into the second, on the air."""
    try:
        old_text, new_text = old.decode(), new.decode()
        delta = unified_delta(old_text, new_text)
    except UnicodeDecodeError:
        delta = None
        print("\n(binary files — skipping text delta methods)")

    rows: list[tuple[str, int]] = [
        ("whole file, raw", len(new)),
        ("whole file, zlib", len(zlib.compress(new, 9))),
    ]
    if delta is not None:
        rows += [
            ("delta, raw", len(delta)),
            ("delta, zlib", len(zlib.compress(delta, 9))),
        ]
    if zstd is not None:
        rows.append(("whole file, zstd", len(zstd.ZstdCompressor(level=19).compress(new))))
        if delta is not None:
            rows.append(("delta, zstd", len(zstd.ZstdCompressor(level=19).compress(delta))))
            rows.append(("delta, zstd + old file as dictionary",
                         len(zstd_with_dict(delta, old))))
        rows.append(("whole file, zstd + old file as dictionary",
                     len(zstd_with_dict(new, old))))

    print("\nwhat it would cost to send the update:")
    print(f"  {'method':44} {'bytes':>7} {'datagrams':>10}")
    best = min(size for _, size in rows)
    for method, size in rows:
        mark = "  <= smallest" if size == best else ""
        print(f"  {method:44} {size:7d} {packets(size):10d}{mark}")

    whole = len(new)
    print(f"\n  sending the whole file raw would take {packets(whole)} datagrams; "
          f"the best method here takes {packets(best)}")
    if zstd is None:
        print("  (install zstandard for the dictionary methods — usually the winners)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("first", help="the older version, or the file that was sent")
    ap.add_argument("second", help="the newer version, or the file that arrived")
    ap.add_argument("--show-diff", action="store_true", help="print the changed lines")
    ap.add_argument("--quiet", action="store_true", help="just say same or different")
    args = ap.parse_args()

    a_path, b_path = Path(args.first), Path(args.second)
    for p in (a_path, b_path):
        if not p.exists():
            raise SystemExit(f"no such file: {p}")

    a, b = a_path.read_bytes(), b_path.read_bytes()

    if args.quiet:
        print("identical" if a == b else "different")
        sys.exit(0 if a == b else 1)

    same = report_identity(a, b, a_path.name, b_path.name)
    if same:
        sys.exit(0)

    try:
        report_similarity(a.decode(), b.decode(), args.show_diff)
    except UnicodeDecodeError:
        print("\n(binary files — skipping line comparison)")

    report_cost(a, b)
    sys.exit(1)


if __name__ == "__main__":
    main()
