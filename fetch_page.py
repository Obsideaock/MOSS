"""Turn a Wikipedia article into a plain-text MOSS page — no images, no chrome.

Run this on your own machine, which has internet. It uses Wikipedia's API,
so you get the article text without navigation, infoboxes, or pictures.

    python fetch_page.py Shrubbies
    python fetch_page.py https://en.wikipedia.org/wiki/MeshCore
    python fetch_page.py Boulder,_Colorado --out boulder.md --no-refs

It prints how many datagrams the page would take to send, raw and compressed,
so you can see what you're about to put on the air before you send it.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

API = "https://{lang}.wikipedia.org/w/api.php"
USER_AGENT = "MOSS-test/0.1 (mesh page fetcher; contact: your-email)"
CHUNK_BYTES = 158          # 163-byte datagram minus the 5-byte MOSS header

# Sections that are mostly links and markup, not reading material.
DROP_SECTIONS = {
    "references", "external links", "see also", "further reading",
    "notes", "bibliography", "sources", "citations",
}


def title_from(argument: str) -> tuple[str, str]:
    """Accept a bare title or a full Wikipedia URL. Returns (title, language)."""
    if "://" not in argument:
        return argument.replace(" ", "_"), "en"
    parsed = urllib.parse.urlparse(argument)
    lang = parsed.netloc.split(".")[0] or "en"
    title = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
    return title, lang


def fetch_extract(title: str, lang: str) -> dict:
    params = {
        "action": "query", "format": "json", "prop": "extracts",
        "explaintext": "1", "redirects": "1", "titles": title,
    }
    url = API.format(lang=lang) + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
    pages = data.get("query", {}).get("pages", {})
    if not pages:
        raise SystemExit(f"no such article: {title}")
    page = next(iter(pages.values()))
    if "missing" in page:
        raise SystemExit(f"no such article: {title}")
    return page


def to_markdown(title: str, body: str, lang: str, drop_refs: bool) -> str:
    """Wikipedia's plain extract uses '== Heading ==' — convert and tidy."""
    out: list[str] = []
    skipping = False

    for line in body.splitlines():
        heading = re.match(r"^(={2,6})\s*(.+?)\s*\1$", line.strip())
        if heading:
            depth = len(heading.group(1))
            name = heading.group(2).strip()
            skipping = drop_refs and name.lower() in DROP_SECTIONS
            if not skipping:
                out.append("")
                out.append("#" * depth + " " + name)
                out.append("")
            continue
        if skipping:
            continue
        out.append(line.rstrip())

    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    url = f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
    header = (f"# {title}\n\n"
              f"*Source: Wikipedia, {url} — text under CC BY-SA 4.0. "
              f"Images and navigation removed.*\n\n")
    return header + text + "\n"


def report(path: Path, text: str) -> None:
    raw = text.encode()
    squeezed = zlib.compress(raw, 9)
    raw_chunks = math.ceil(len(raw) / CHUNK_BYTES)
    zip_chunks = math.ceil(len(squeezed) / CHUNK_BYTES)
    print(f"\nwrote {path}")
    print(f"  {len(raw):,} bytes  -> {raw_chunks} datagrams")
    print(f"  {len(squeezed):,} bytes compressed -> {zip_chunks} datagrams "
          f"({len(squeezed)/len(raw):.0%} of the original)")
    minutes = zip_chunks * 1.55 / 60
    print(f"  roughly {minutes:.1f} min to send with --zlib at the default gap")
    print(f"\n  python page_transfer.py --channel 1 --send {path.name} --zlib")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("article", help="title or full Wikipedia URL")
    ap.add_argument("--out", help="output file (default: the article's title)")
    ap.add_argument("--no-refs", action="store_true",
                    help="drop References, External links, See also and friends")
    ap.add_argument("--max-bytes", type=int,
                    help="truncate at roughly this size, on a paragraph break")
    args = ap.parse_args()

    title, lang = title_from(args.article)
    print(f"fetching {title} from {lang}.wikipedia.org ...")
    page = fetch_extract(title, lang)
    body = page.get("extract", "")
    if not body.strip():
        raise SystemExit("the article came back empty")

    text = to_markdown(page.get("title", title), body, lang, args.no_refs)

    if args.max_bytes and len(text.encode()) > args.max_bytes:
        cut = text.encode()[:args.max_bytes].decode(errors="ignore")
        cut = cut.rsplit("\n\n", 1)[0]
        text = cut + "\n\n*(truncated)*\n"

    name = args.out or re.sub(r"[^A-Za-z0-9._-]", "_", page.get("title", title)) + ".md"
    path = Path(name)
    path.write_text(text, encoding="utf-8")
    report(path, text)


if __name__ == "__main__":
    main()
