"""Command line for MOSS.

  Just type  moss  for a menu of everything below. Install with one line
  (see README), then  moss setup  remembers your radio, so --port is optional.

  Seed Vault, the gateway (radio + a library: a Kiwix .zim file, Wikipedia online, or a folder):
    python -m moss gateway --port /dev/ttyACM0 --source /media/moss/KIWIX/wikipedia_en_all_nopic_2026-06.zim
    python -m moss gateway --port COM16                             # live Wikipedia (needs internet)

  Reader (radio only):
    python -m moss search "shrubbies band" --port COM12
    python -m moss read "Boulder, Colorado" --port COM12
    python -m moss web --port COM12                      # browser reader

  Everything happens on the #moss channel, which MOSS finds or creates on
  the radio by itself.
    python -m moss cache                                  # what's saved (no radio)
    python -m moss cache show "Boulder, Colorado"         # read it offline
    python -m moss export ~/Documents/MyVault             # saved articles -> Obsidian notes

  Check a library without any radio (run it on the Pi):
    python -m moss library /media/moss/KIWIX/wikipedia_en_all_nopic_2026-06.zim "boulder colorado"

  Community pages (no radio needed yet; uploading comes next):
    python -m moss author new "Your Name"                 # make your author identity
    python -m moss page make water-filters.md             # sign a page, ready to upload
    python -m moss page make notes.md --unlisted --anonymous

  No hardware? Run a pretend radio with a gateway built in:
    python -m moss web --sim TestExamples --sim-loss 0.1
    python -m moss web --sim TestExamples/mini_wikipedia.zim       # or any .zim
"""

from __future__ import annotations

import argparse
import inspect
import asyncio
import contextlib
import pydoc
import re
import shutil
import sys
import textwrap
import time
from pathlib import Path

from . import protocol as P
from .cache import Cache
from .client import MossClient, MossError, Progress
from .gateway import Gateway
from .pagestore import Limits, PageStore
from .sporeprint import VaultPrint as VaultPrintSim
from .sources import LocalSource, WikipediaSource
from .transport import MeshCoreTransport, SimNetwork
from . import seal as S

from .config import moss_home as _moss_home
DEFAULT_VAULT_KEYS = _moss_home() / "seed-vault"

B, DIM, RST = "\033[1m", "\033[2m", "\033[0m"


# ---------------------------------------------------------------- terminal progress

def _secs(n: float) -> str:
    n = int(max(0, n))
    return f"{n}s" if n < 60 else f"{n // 60}m{n % 60:02d}s"


class TermProgress:
    """One live line on stderr: what's happening, a packet bar, a countdown."""

    SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self):
        self.tty = sys.stderr.isatty()
        self.n = 0
        self.last_text = ""

    @staticmethod
    def describe(p: Progress) -> str:
        got = len(p.have)
        if p.stage == "waiting":
            return "waiting for the radio"
        if p.stage == "requesting" and p.message:
            return p.message + (f" (try {p.attempt})" if p.attempt > 1 else "")
        if p.stage == "requesting":
            return ("asking the Seed Vault" if p.attempt <= 1
                    else f"no pulse yet, asking again (try {p.attempt})")
        if p.stage == "queued":
            return (f"dormant behind {p.queue_position} other request(s)"
                    if p.queue_position else "next in line at the Seed Vault")
        if p.stage == "fetching":
            return "the Seed Vault is looking it up"
        if p.stage == "verifying":
            return "checking every byte"
        if p.stage == "done":
            return "done"
        if p.stage == "uploading":
            return "sending spores"
        if p.stage == "checking":
            return "the Seed Vault is checking the page"
        left = p.total - got
        text = "repairing" if p.stage == "repairing" else "receiving"
        if p.stage == "repairing":
            text += f" ({left} spore(s) in bare patches)"
        return text

    @staticmethod
    def bar(p: Progress, width: int = 36) -> str:
        if not p.total:
            return ""
        have = set(p.have)
        cells = min(width, p.total)
        out = []
        for c in range(cells):
            lo, hi = c * p.total // cells, (c + 1) * p.total // cells
            got = sum(1 for i in range(lo, max(hi, lo + 1)) if i in have)
            span = max(hi, lo + 1) - lo
            out.append("█" if got == span else "▒" if got else "·")
        return "".join(out)

    def __call__(self, p: Progress) -> None:
        text = self.describe(p)
        if p.total:
            text += f"  {self.bar(p)}  {len(p.have)}/{p.total}"
        if p.eta is not None and p.total and len(p.have) < p.total:
            text += f"  ~{_secs(p.eta)} left"
        text += f"  [{_secs(p.elapsed)}]"
        if self.tty:
            self.n += 1
            sys.stderr.write(f"\r\033[K{self.SPIN[self.n % len(self.SPIN)]} {text}")
            sys.stderr.flush()
        elif p.stage != self.last_text:
            print(text, file=sys.stderr)
        self.last_text = p.stage

    def finish(self) -> None:
        if self.tty:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()


# ---------------------------------------------------------------- reading view

def _plain(s: str) -> str:
    """Markup a terminal can't show: [[Target|text]] -> text, **b** / *i* -> b / i."""
    s = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"(?<!\\)\*+", "", s)
    return s.replace("\\$", "$").replace("\\*", "*")


def render_article(text: str, width: int | None = None, color: bool = True) -> str:
    from . import tables
    width = min(width or shutil.get_terminal_size().columns, 84) - 2
    out: list[str] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            joined = " ".join(para)
            note = re.fullmatch(r"\*[^*].*[^*]\*", joined.strip())
            wrapped = textwrap.fill(_plain(joined), width)
            out.append(f"{DIM}{wrapped}{RST}" if note and color else wrapped)
            para.clear()

    for kind, piece in tables.blocks(text):
        if kind == "table":
            flush()
            lines = [_plain(line) for line in tables.to_text(piece)]
            title = "Key facts" if piece.kind == "facts" else ""
            body = "\n".join(textwrap.shorten(line, width * 3, placeholder=" …")
                             if len(line) > width * 3 else line for line in lines)
            out.append((f"{B}{title}{RST}\n" if title and color else (title + "\n" if title else ""))
                       + body)
            continue
        for raw in piece.splitlines():
            line = raw.rstrip()
            m = re.match(r"^(#{1,6})\s+(.*)$", line)
            if m:
                flush()
                title = m.group(2)
                if m.group(1) == "#":
                    out.append(f"{B}{title}{RST}\n{'═' * min(len(title), width)}" if color
                               else f"{title}\n{'=' * min(len(title), width)}")
                else:
                    out.append(f"{B}{title}{RST}" if color else f"{title}\n{'-' * len(title)}")
            elif re.match(r"^ *(?:[-*]|\d+\.) ", line):
                flush()
                out.append(_plain(line))
            elif not line.strip():
                flush()
            else:
                para.append(line.strip())
        flush()
    return "\n\n".join(out) + "\n"


def show_article(text: str, pager: bool) -> None:
    color = sys.stdout.isatty()
    body = render_article(text, color=color)
    if pager and sys.stdout.isatty() and body.count("\n") > shutil.get_terminal_size().lines:
        pydoc.pager(body)
    else:
        print(body)


# ---------------------------------------------------------------- connecting

def _old_channel_flag(args) -> None:
    if getattr(args, "channel", None) is not None:
        print(f"  note: --channel {args.channel} is no longer needed and is ignored; MOSS now finds "
              f"(or creates) the {args.channel_name} channel on the radio by itself.",
              file=sys.stderr, flush=True)

def open_source(spec: str, lang: str, max_bytes: int | None, links: bool = True):
    """'wikipedia', a .zim file, or a folder of .md files -> (source, label)."""
    if spec == "wikipedia":
        return WikipediaSource(lang, max_bytes), f"{lang}.wikipedia.org (live)"
    path = Path(spec).expanduser()
    if path.suffix.lower() == ".zim":
        from .zim import ZimSource
        source = ZimSource(path, max_bytes, links=links)
        return source, source.describe()
    if not path.is_dir():
        raise SystemExit(f"--source: {spec} is not 'wikipedia', a .zim file or a folder")
    return LocalSource(path, max_bytes), f"folder {path}"


@contextlib.asynccontextmanager
async def connect(args):
    """Yield (client, info). Opens a real radio, or a simulated one with a
    gateway attached when --sim is given."""
    cache = Cache(args.cache)
    timing = dict(quiet=args.quiet, idle=args.idle)
    if args.sim:
        import tempfile
        net = SimNetwork(loss=args.sim_loss, airtime=args.sim_airtime, seed=args.sim_seed)
        # A practice vault with throwaway keys, trusted only for this run: it
        # must never end up pinned in place of your real Seed Vault.
        practice = Path(tempfile.mkdtemp(prefix="moss-sim-"))
        keyring = None if args.plain else S.Keyring(practice / "reader")
        gateway = Gateway(net.endpoint("gateway"), open_source(args.sim, "en", args.max_bytes, not args.no_links)[0],
                          gap=args.sim_airtime, wait=args.sim_wait, settle=0.3,
                          log=lambda m: print(f"  seed vault: {m}", file=sys.stderr),
                          keys=S.VaultKeys.load_or_create(practice / "vault", "Practice Seed Vault"),
                          pad_sizes=args.pad_sizes, pages=PageStore(practice / "pages"),
                          upload_quiet=1.5, library=open_source(args.sim, "en", None)[1],
                          spore_print=VaultPrintSim(practice / "vault"))
        await gateway.start()
        client = MossClient(net.endpoint("client"), cache, keyring=keyring, nack_spacing=0.2,
                            **{**timing, "quiet": min(args.quiet, 1.5)})
        info = {"simulated": True,
                "label": f"Simulated radio, {args.sim_loss:.0%} of spores lost"}
        try:
            yield client, info
        finally:
            await gateway.stop()
        return

    _old_channel_flag(args)
    tx = await MeshCoreTransport.open(args.port, args.baud, args.channel_name, args.data_type,
                                      log=lambda m: print(f"\n  {m}", file=sys.stderr),
                                      scope=args.scope, ble=args.ble, pin=args.pin,
                                      route=args.route)
    keyring = None if args.plain else S.Keyring()
    client = MossClient(tx, cache, keyring=keyring,
                        notice=lambda m: print(f"\n  {m}", file=sys.stderr), **timing)
    info = {"simulated": False, "label": ("Bluetooth radio" if tx.ble is not None else "Radio")
                                          + f" on {tx.channel_name}"
                                          + (f", scope {args.scope}" if tx.scoped else "")}
    try:
        yield client, info
    finally:
        await tx.close()


# ---------------------------------------------------------------- commands

def _pick_from(results, args, community: list[dict] | None = None) -> str | None:
    """Show both groups, numbered on; returns a Wikipedia title or a moss: address."""
    b, dim, rst = (B, DIM, RST) if sys.stdout.isatty() else ("", "", "")
    picks: list[str] = []
    if results and community:
        print(f"{b}Wikipedia{rst}")
    for r in results:
        picks.append(r.title)
        print(f"  {b}{len(picks)}{rst}  {r.title}" + (f"\n     {dim}{r.blurb}{rst}" if r.blurb else ""))
    if community:
        print(f"{b}Community{rst}")
        for c in community:
            picks.append(c["a"])
            who = f"{c['n'] or 'anonymous'} · {c['f']}"
            print(f"  {b}{len(picks)}{rst}  {c['t']}  {dim}by {who}{rst}"
                  + (f"\n     {dim}{c['d']}{rst}" if c.get("d") else ""))
    if not picks:
        print("nothing found")
        return None
    if args.pick is not None:
        return picks[args.pick - 1] if 1 <= args.pick <= len(picks) else None
    if not sys.stdin.isatty():
        return None
    while True:
        answer = input(f"\nRead which? [1-{len(picks)}, q to quit] ").strip().lower()
        if answer in ("q", "quit", ""):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(picks):
            return picks[int(answer) - 1]
        print("  pick a number from the list")


async def cmd_search(args) -> int:
    cache = Cache(args.cache)
    query = " ".join(args.query)
    hit = None if args.refresh else cache.get_search(query)
    progress = TermProgress()
    if hit:
        results = hit[0]
        dim, rst = (DIM, RST) if sys.stdout.isatty() else ("", "")
        print(f"{dim}(saved search, no radio needed){rst}")
        title = _pick_from(results, args, cache.get_community(query))
        if title and title.startswith("moss:"):
            from .pages_cli import _open
            args.address = title
            return await _open(args)
        return await _read(args, title) if title else 0
    async with connect(args) as (client, _):
        try:
            out = await client.search(query, args.refresh, progress)
        except MossError as exc:
            progress.finish()
            print(f"search failed: {exc}", file=sys.stderr)
            return 1
        progress.finish()
        title = _pick_from(out.results, args, out.community)
        if not title:
            return 0
        if title.startswith("moss:"):
            return await _show_page(client, title, args)
        return await _read_with(client, title, args)


async def _show_page(client: MossClient, address: str, args) -> int:
    progress = TermProgress()
    try:
        got = await client.page(address, args.refresh, progress)
    except MossError as exc:
        progress.finish()
        print(f"couldn't get {address}: {exc}", file=sys.stderr)
        return 1
    progress.finish()
    page = got.page
    client.social.note_opened(address)
    print(f"{page.describe()}\n  signature checked\n")
    if page.deleted:
        print("This page was deleted by its author.")
    else:
        show_article(page.text(), not args.no_pager)
    return 0


async def _read_with(client: MossClient, title: str, args) -> int:
    progress = TermProgress()
    try:
        out = await client.article(title, args.refresh, progress)
    except MossError as exc:
        progress.finish()
        print(f"couldn't get {title!r}: {exc}", file=sys.stderr)
        return 1
    progress.finish()
    dim, rst = (DIM, RST) if sys.stdout.isatty() else ("", "")
    print(f"\n{dim}{'saved copy' if out.from_cache else 'received over radio'}{rst}\n")
    show_article(out.text, not args.no_pager)
    return 0


async def _read(args, title: str) -> int:
    cache = Cache(args.cache)
    if not args.refresh and (hit := cache.get_article(title)):
        dim, rst = (DIM, RST) if sys.stdout.isatty() else ("", "")
        print(f"\n{dim}saved copy, no radio needed{rst}\n")
        show_article(hit[1], not args.no_pager)
        return 0
    async with connect(args) as (client, _):
        return await _read_with(client, title, args)


async def cmd_read(args) -> int:
    return await _read(args, " ".join(args.title))


async def cmd_cache(args) -> int:
    cache = Cache(args.cache)
    if args.action == "show":
        title = " ".join(args.title)
        hit = cache.get_article(title)
        if not hit:
            print(f"not saved: {title}", file=sys.stderr)
            return 1
        show_article(hit[1], not args.no_pager)
    elif args.action == "forget":
        title = " ".join(args.title)
        print("removed" if cache.forget(title) else f"not saved: {title}")
    else:
        items = cache.list_articles()
        if not items:
            print(f"nothing saved yet ({cache.folder})")
        for a in items:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(a.fetched_at))
            print(f"  {a.title:<40} {a.bytes / 1024:6.1f} KB   {when}")
    return 0


async def cmd_export(args) -> int:
    from .vault import export_vault
    cache = Cache(args.cache)
    vault = Path(args.vault).expanduser()
    if not vault.is_dir():
        raise SystemExit(f"not a folder: {vault}")
    # Titles from past searches become links too; Obsidian shows the ones
    # you haven't fetched yet as unresolved notes in the graph.
    extra: list[str] = []
    if args.link_searches:
        import json
        try:
            searches = json.loads((cache.folder / "searches.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            searches = {}
        extra = [r["title"] for s in searches.values() for r in s["results"]]
    written = export_vault(cache, vault, args.folder, extra)
    if not written:
        print(f"nothing saved yet ({cache.folder}); fetch an article first")
        return 0
    print(f"wrote {len(written)} note(s) to {vault / args.folder}")
    for p in written:
        print(f"  {p.name}")
    return 0


async def cmd_gateway(args) -> int:
    source, what = open_source(args.source, args.lang, args.max_bytes, not args.no_links)
    _old_channel_flag(args)
    keys = S.VaultKeys.load_or_create(args.keys or DEFAULT_VAULT_KEYS, args.name)
    tx = await MeshCoreTransport.open(args.port, args.baud, args.channel_name, args.data_type,
                                      log=lambda m: print(f"  {m}", file=sys.stderr, flush=True),
                                      scope=args.scope, ble=args.ble, pin=args.pin, route=args.route)
    limits = Limits(cap_spores=args.page_cap, per_author_per_day=args.uploads_per_author,
                    spores_per_day=args.upload_budget, storage_bytes=args.page_storage_mb * 1024 ** 2,
                    accept_uploads=not args.no_uploads)
    store = PageStore(Path(args.pages).expanduser() if args.pages
                      else Path(args.keys or DEFAULT_VAULT_KEYS).expanduser() / "pages", limits)
    from .sporeprint import VaultPrint, describe as print_describe, summary as print_summary
    vprint = VaultPrint(Path(args.keys or DEFAULT_VAULT_KEYS))
    gw = Gateway(tx, source, gap=args.gap, rounds=args.rounds, wait=args.wait, keys=keys,
                 pad_sizes=args.pad_sizes, log_queries=args.log_queries, pages=store,
                 library=what, spore_print=vprint, log=lambda m: print(m, flush=True))
    size = (f"articles shortened to about {args.max_bytes} bytes" if args.max_bytes
            else "full articles")
    where = f"{tx.channel_name} (slot {tx.channel})" + (f", scope {args.scope}" if tx.scoped else "")
    print(f"Seed Vault up on {where}, serving {what}, {size}. Ctrl+C to stop.")
    print(f"  fingerprint {keys.fingerprint}  (readers should see the same one)"
          + ("; page sizes padded" if args.pad_sizes else ""), flush=True)
    if args.route:
        print("  routing on: spores go back along the route a request came by, when it's known",
              flush=True)
    sp = vprint.get()
    print(f"  spore print: {print_describe(print_summary(sp)) if sp else 'none yet'}", flush=True)
    n = len(list(store.folder.glob("*/*.mosspage")))
    print(f"  community pages: {n} stored in {store.folder}; " +
          (f"uploads on (up to {limits.cap_spores} spores, {limits.per_author_per_day} per author "
           f"and {limits.spores_per_day} spores in all per day)" if limits.accept_uploads
           else "uploads off"), flush=True)
    try:
        await gw.run_forever()
    finally:
        await gw.stop()
        await tx.close()
    return 0


async def cmd_library(args) -> int:
    """Search a source directly and show what an article would cost on air."""
    import math
    import zlib
    source, what = open_source(args.source, args.lang, args.max_bytes, not args.no_links)
    print(what)
    query = " ".join(args.query)
    started = time.monotonic()
    results = await source.search(query)
    print(f"{len(results)} result(s) in {time.monotonic() - started:.1f}s\n")
    if not results:
        return 1
    title = _pick_from(results, args)
    if not title:
        return 0
    started = time.monotonic()
    real, text = await source.article(title)
    took = time.monotonic() - started
    raw = text.encode()
    packed = min(len(zlib.compress(raw, 9)), len(raw))
    chunks = math.ceil(packed / P.CHUNK_BYTES)
    seconds = chunks * (args.gap + 0.55)
    print()
    show_article(text, not args.no_pager)
    dim, rst = (DIM, RST) if sys.stdout.isatty() else ("", "")
    print(f"{dim}{real}: looked up in {took:.1f}s, {len(raw):,} bytes -> {packed:,} "
          f"compressed -> {chunks} spores, about {_secs(seconds)} on air{rst}")
    return 0


async def cmd_vault_key(args) -> int:
    """On the Pi: show (creating if needed) the Seed Vault's fingerprint."""
    keys = S.VaultKeys.load_or_create(args.keys or DEFAULT_VAULT_KEYS)
    print(f"Seed Vault fingerprint: {keys.fingerprint}")
    print(f"keys in {Path(args.keys or DEFAULT_VAULT_KEYS).expanduser()} (back these up; "
          f"new keys mean every reader sees a new fingerprint)")
    return 0


async def cmd_trust(args) -> int:
    """On a laptop: which Seed Vault this reader trusts."""
    ring = S.Keyring()
    if args.forget:
        ring.forget()
        print("forgotten; the next Seed Vault heard will be trusted (or pin one first)")
    elif args.fingerprint:
        try:
            ring.pin(args.fingerprint)
        except ValueError as exc:
            raise SystemExit(str(exc))
        print(f"will only trust the Seed Vault with fingerprint {ring.fingerprint}")
    elif "x" in ring.data:
        print(f"trusting {ring.data.get('name', 'Seed Vault')} {ring.fingerprint} "
              f"(since {ring.data.get('since', '?')})")
    elif "expect" in ring.data:
        print(f"waiting to meet the Seed Vault with fingerprint {ring.fingerprint}")
    else:
        print("no Seed Vault trusted yet; the first one heard will be (or pin one: moss trust FINGERPRINT)")
    return 0


def cmd_menu(args) -> int:
    from . import menu
    return menu.main()


def cmd_setup(args) -> int:
    from . import setup
    return setup.main_from_args(args)


def cmd_update(args) -> int:
    from . import updater
    try:
        updater.update(args.branch)
    except updater.UpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


async def cmd_vault_info(args) -> int:
    """A reader asks the Seed Vault about itself (and trades spore prints)."""
    from . import sporeprint as SP
    async with connect(args) as (client, info):
        progress = TermProgress()
        try:
            specs = await client.specs(progress)
        except MossError as exc:
            progress.finish()
            print(f"error: {exc}", file=sys.stderr)
            return 1
        progress.finish()
    for line in describe_specs(specs):
        print(line)
    mine = client.prints.mine()
    if mine:
        print(f"\nthis reader first sprouted from: {SP.describe(SP.summary(mine))}")
    return 0


def describe_specs(specs: dict) -> list[str]:
    """Vault specs as lines of text (shared by the CLI and the menu)."""
    from . import sporeprint as SP
    from . import PROTOCOL
    out = [f"{specs.get('name', 'Seed Vault')}  {specs.get('fingerprint', '')}",
           f"  MOSS {specs.get('moss', '?')}" + ("" if specs.get("protocol", 0) >= PROTOCOL
                                                  else "  (older than this reader: moss update on the Pi)"),
           f"  library: {specs.get('library') or 'not said'}"]
    c = specs.get("community")
    if c:
        out.append(f"  community pages: {c['public_pages']} public; uploads "
                   + (f"on (up to {c['cap_spores']} spores, {c['per_author_per_day']} per author and "
                      f"{c['spores_per_day']} spores in all per day)" if c["uploads"] else "off"))
    st = specs.get("storage")
    if st:
        out.append(f"  storage: {st['free_mb']} MB free for community pages "
                   f"({st['used_mb']} of {st['cap_mb']} MB used; {st['disk_free_mb']} MB free on disk)")
    sp = specs.get("spore_print")
    out.append(f"  spore print: {SP.describe(sp) if sp else 'none yet'}")
    if specs.get("print_exchange"):
        out.append(f"  ({specs['print_exchange']})")
    return out


def cmd_spore_print(args) -> int:
    """On the Seed Vault: show its spore print, or start the original one."""
    from . import sporeprint as SP
    keys = S.VaultKeys.load_or_create(args.keys or DEFAULT_VAULT_KEYS)
    vp = SP.VaultPrint(Path(args.keys or DEFAULT_VAULT_KEYS))
    if args.note:
        try:
            sp = vp.start_original(keys, " ".join(args.note))
        except SP.PrintError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        s = SP.summary(sp)
        print(f"this vault is now an original: {SP.describe(s)}")
        print(f"its root fingerprint is {s['origin']}; readers carry this print to vaults that have none")
        print("restart the Seed Vault so it serves it: sudo systemctl restart moss-gateway")
        return 0
    sp = vp.get()
    print(SP.describe(SP.summary(sp)) if sp else
          "no spore print yet. Readers bring one the first time they visit with one; "
          "or start an original: moss spore-print \"a note about this vault\"")
    return 0


async def cmd_selftest(args) -> int:
    import tempfile
    from . import selftest
    args.cache = tempfile.mkdtemp(prefix="moss-selftest-cache-")   # leave saved articles alone
    query = args.query or ("Shrubbies" if args.sim else "Colorado")
    async with connect(args) as (client, info):
        print(f"MOSS self-test over {info['label']}\n")
        ok = await selftest.run(client, query=query)
    return 0 if ok else 1


async def cmd_radio_link(args) -> int:
    from . import radiocheck
    if args.public:
        freq, bw, sf, cr = radiocheck.PUBLIC_US
    elif len(args.settings) == 4:
        try:
            freq, bw, sf, cr = float(args.settings[0]), float(args.settings[1]), int(args.settings[2]), int(args.settings[3])
        except ValueError:
            print("usage: moss radio-link FREQ BW SF CR   (e.g. 915.0 62.5 7 5), or --public")
            return 1
    else:
        print("usage: moss radio-link FREQ BW SF CR   (e.g. 915.0 62.5 7 5), or --public")
        return 1
    for note in radiocheck.check_link(freq, bw, sf, cr):
        print(note)
    link = radiocheck.Link(args.port, args.ble, args.pin)
    return 0 if await radiocheck.set_link(link, freq, bw, sf, cr) else 1


async def cmd_radio_check(args) -> int:
    from . import radiocheck
    link = radiocheck.Link(args.port, args.ble, args.pin)
    if args.action == "send":
        await radiocheck.send(link, args.count, args.gap)
    elif args.action == "listen":
        await radiocheck.listen(link, args.seconds)
    else:
        await radiocheck.info(link)
    return 0


async def cmd_web(args) -> int:
    from . import web
    async with connect(args) as (client, info):
        await web.serve(client, info, args.host, args.http_port, not args.no_open)
    return 0


# ---------------------------------------------------------------- argument parsing

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="moss", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    from . import config
    saved = config.load()                    # what `moss setup` remembered; flags still win
    common = argparse.ArgumentParser(add_help=False)
    class _Port(argparse.Action):           # an explicit --port beats a saved Bluetooth radio
        def __call__(self, parser, ns, value, option=None):
            ns.port, ns.ble = value, None

    common.add_argument("--port", default=saved.get("port"), action=_Port,
                        help="serial port of your radio (default: the one setup saved, "
                             "else auto-picked if only one)")
    common.add_argument("--ble", nargs="?", const="", default=saved.get("ble"), metavar="ADDRESS",
                        help="connect over Bluetooth instead of USB; give the radio's address or "
                             "name, or nothing for the first MeshCore radio found")
    common.add_argument("--pin", default=saved.get("pin"),
                        help="Bluetooth pairing PIN, if the radio asks for one")
    common.add_argument("--baud", type=int, default=115200)
    common.add_argument("--channel-name", default=saved.get("channel") or "#moss",
                        help="the hashtag channel MOSS uses (default #moss); found or created on the radio")
    common.add_argument("--scope", default=saved.get("scope"), help="MeshCore region to keep flooded traffic inside "
                                        "(e.g. a name your local repeaters use; needs firmware 1.12+)")
    common.add_argument("--data-type", type=lambda s: int(s, 0), default=P.MOSS_DATA_TYPE)
    common.add_argument("--route", action=argparse.BooleanOptionalAction, default=bool(saved.get("route")),
                        help="send spores along the route the other side's messages came by, instead "
                             "of flooding every repeater (opt-in; falls back to flooding)")
    common.add_argument("--debug", action="store_true",
                        help="print everything the MeshCore library does (for diagnosing radio trouble)")
    # Old flags, still accepted so existing setups (like the Pi's service) keep working.
    common.add_argument("--channel", type=int, help=argparse.SUPPRESS)
    common.add_argument("--yes-really-public", action="store_true", help=argparse.SUPPRESS)
    common.add_argument("--cache", help="cache folder (default ~/.moss/cache)")

    client = argparse.ArgumentParser(add_help=False, parents=[common])
    client.add_argument("--refresh", action="store_true", help="ask over radio even if saved")
    client.add_argument("--quiet", type=float, default=6.0,
                        help="seconds of silence before asking for missing spores (bare patches)")
    client.add_argument("--idle", type=float, default=120.0,
                        help="give up after this many seconds of silence mid-transfer")
    client.add_argument("--no-pager", action="store_true")
    client.add_argument("--plain", action="store_true",
                        help="debugging only: send requests and receive pages unencrypted")
    sim = client.add_argument_group("pretend radio (no hardware)")
    sim.add_argument("--sim", metavar="FOLDER_OR_ZIM",
                     help="serve articles from a folder of .md files or a .zim over a simulated radio")
    sim.add_argument("--sim-loss", type=float, default=0.1, help="fraction of spores lost (default 0.1)")
    sim.add_argument("--sim-airtime", type=float, default=0.15, help="seconds per spore (default 0.15)")
    sim.add_argument("--sim-wait", type=float, default=6.0)
    sim.add_argument("--sim-seed", type=int)
    sim.add_argument("--no-links", action="store_true", help="leave out links between articles")
    sim.add_argument("--pad-sizes", action="store_true", help="practice vault pads page sizes")
    sim.add_argument("--max-bytes", type=int, default=0,
                     help="shorten articles to about this many bytes (default 0 = full articles)")

    sub = ap.add_subparsers(dest="cmd")       # no command: the menu

    g = sub.add_parser("gateway", parents=[common], help="run the Seed Vault: answer requests from a .zim, Wikipedia or a folder")
    g.add_argument("--source", default="wikipedia",
                   help="a Kiwix .zim file, 'wikipedia' (live, needs internet), or a folder of .md files")
    g.add_argument("--lang", default="en")
    g.add_argument("--max-bytes", type=int, default=0,
                   help="shorten articles to about this size to save airtime (default 0 = full articles)")
    g.add_argument("--no-links", action="store_true",
                   help="leave out links between articles (saves roughly 10-25%% airtime)")
    g.add_argument("--gap", type=float, default=1.0, help="seconds between spores")
    g.add_argument("--keys", help=f"folder for the Seed Vault's keys (default {DEFAULT_VAULT_KEYS})")
    g.add_argument("--name", default="Seed Vault", help="name readers see when they first meet this vault")
    g.add_argument("--pad-sizes", action="store_true",
                   help="round encrypted pages up to standard sizes so their length reveals less "
                        "(about 10%% more airtime; off by default)")
    g.add_argument("--log-queries", action="store_true",
                   help="write the titles of encrypted requests to the log (off: no record of what people read)")
    g.add_argument("--rounds", type=int, default=10,
                   help="repair rounds before giving up; each only resends what's missing")
    g.add_argument("--pages", help="folder for community pages (default: next to the keys, "
                                   "~/.moss/seed-vault/pages)")
    g.add_argument("--no-uploads", action="store_true",
                   help="serve community pages already here, but take no new uploads")
    g.add_argument("--page-cap", type=int, default=250, help="biggest page accepted, in spores (default 250)")
    g.add_argument("--uploads-per-author", type=int, default=3, help="uploads per author per day (default 3)")
    g.add_argument("--upload-budget", type=int, default=2000,
                   help="spores of uploads per day for the whole vault (default 2000, about 55 minutes of airtime)")
    g.add_argument("--page-storage-mb", type=int, default=2048, help="room for community pages (default 2048 MB)")
    g.add_argument("--wait", type=float, default=30.0,
                   help="seconds to keep listening for the reader's bare-patch list "
                        "(about 5x the client's --quiet, so a few lost lists don't end a transfer)")
    g.set_defaults(fn=cmd_gateway)

    s = sub.add_parser("search", parents=[client], help="search, pick, read")
    s.add_argument("query", nargs="+")
    s.add_argument("--pick", type=int, help="read this result without asking")
    s.set_defaults(fn=cmd_search)

    r = sub.add_parser("read", parents=[client], help="read an article by title")
    r.add_argument("title", nargs="+")
    r.set_defaults(fn=cmd_read)

    w = sub.add_parser("web", parents=[client], help="browser reader")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--http-port", type=int, default=8765)
    w.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    w.set_defaults(fn=cmd_web)

    c = sub.add_parser("cache", parents=[common], help="list, show or forget saved articles")
    c.add_argument("action", nargs="?", choices=["list", "show", "forget"], default="list")
    c.add_argument("title", nargs="*")
    c.add_argument("--no-pager", action="store_true")
    c.set_defaults(fn=cmd_cache)

    lib = sub.add_parser("library", help="search a .zim (or folder) directly, no radio")
    lib.add_argument("source", help="a .zim file, a folder of .md files, or 'wikipedia'")
    lib.add_argument("query", nargs="+")
    lib.add_argument("--pick", type=int, help="show this result without asking")
    lib.add_argument("--lang", default="en")
    lib.add_argument("--max-bytes", type=int, default=0,
                     help="shorten to about this many bytes (default 0 = full article)")
    lib.add_argument("--gap", type=float, default=1.0, help="Seed Vault gap between spores, for the airtime estimate")
    lib.add_argument("--no-links", action="store_true", help="leave out links between articles")
    lib.add_argument("--no-pager", action="store_true")
    lib.set_defaults(fn=cmd_library)

    vk = sub.add_parser("vault-key", help="on the Pi: show the Seed Vault's fingerprint")
    vk.add_argument("--keys", help=f"keys folder (default {DEFAULT_VAULT_KEYS})")
    vk.set_defaults(fn=cmd_vault_key)

    tr = sub.add_parser("trust", help="on a laptop: show, pin or forget the trusted Seed Vault")
    tr.add_argument("fingerprint", nargs="?", help="only trust the vault with this fingerprint")
    tr.add_argument("--forget", action="store_true", help="forget the trusted vault")
    tr.set_defaults(fn=cmd_trust)

    x = sub.add_parser("export", parents=[common],
                       help="write saved articles into an Obsidian vault")
    x.add_argument("vault", help="path to your Obsidian vault folder")
    x.add_argument("--folder", default="Wikipedia", help="subfolder inside the vault")
    x.add_argument("--link-searches", action="store_true",
                   help="also link titles that only appeared in search results")
    x.set_defaults(fn=cmd_export)

    from .pages_cli import add_parsers
    add_parsers(sub, client)

    m = sub.add_parser("menu", help="the interactive menu (same as plain `moss`)")
    m.set_defaults(fn=cmd_menu)

    st = sub.add_parser("setup", help="set this machine up as a reader or a Seed Vault")
    st.add_argument("--role", choices=["reader", "vault"])
    st.add_argument("--port", help="the radio's port (asked if left out)")
    st.add_argument("--ble", nargs="?", const="", metavar="ADDRESS",
                    help="the radio is on Bluetooth (its address or name; nothing = any MeshCore radio)")
    st.add_argument("--pin", help="Bluetooth pairing PIN, if needed")
    st.add_argument("--zim", help="Seed Vault: the .zim library (asked if left out)")
    st.add_argument("--branch", choices=["main", "experimental"], help="which branch `moss update` follows")
    st.add_argument("--yes", action="store_true", help="don't ask anything; use what's given or saved")
    st.add_argument("--no-service", action="store_true", help="Seed Vault: don't install the background service")
    st.set_defaults(fn=cmd_setup)

    up = sub.add_parser("update", help="get the newest MOSS from GitHub")
    up.add_argument("--branch", choices=["main", "experimental"],
                    help="switch to this branch (remembered); default: the one you follow now")
    up.set_defaults(fn=cmd_update)

    vi = sub.add_parser("vault-info", parents=[client],
                        help="what your Seed Vault says about itself: version, library, free storage, spore print")
    vi.set_defaults(fn=cmd_vault_info)

    sp = sub.add_parser("spore-print", help="on the Seed Vault: show its spore print, or start the original")
    sp.add_argument("note", nargs="*", help="only for an original vault: the note, e.g. "
                                            "\"The original Seed Vault, ATLAS, CU Boulder\"")
    sp.add_argument("--keys", help=f"keys folder (default {DEFAULT_VAULT_KEYS})")
    sp.set_defaults(fn=cmd_spore_print)

    stp = sub.add_parser("selftest", parents=[client],
                         help="end-to-end test with the Seed Vault: search, upload, read back, replay, delete")
    stp.add_argument("--query", default=None, help="what to search for in step 1 (default Colorado)")
    stp.set_defaults(fn=cmd_selftest)

    rl = sub.add_parser("radio-link", parents=[common],
                        help="put this radio on a dedicated link (its own frequency), or back on the public mesh")
    rl.add_argument("settings", nargs="*", metavar="FREQ BW SF CR",
                    help="e.g. 915.0 62.5 7 5 (all MOSS radios must match)")
    rl.add_argument("--public", action="store_true",
                    help="back to MeshCore's usual US settings (910.525 62.5 7 5)")
    rl.set_defaults(fn=cmd_radio_link)

    rc = sub.add_parser("radio-check", parents=[common],
                        help="send or listen for pings on #moss to test range (pauses a Seed Vault)")
    rc.add_argument("action", choices=["send", "listen", "info"])
    rc.add_argument("--count", type=int, default=10)
    rc.add_argument("--gap", type=float, default=3.0, help="seconds between pings")
    rc.add_argument("--seconds", type=float, default=300, help="how long to listen")
    rc.set_defaults(fn=cmd_radio_check)
    return ap


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.cmd is None:
        args.fn = cmd_menu
    from .pages_cli import fix_args
    fix_args(args)
    if getattr(args, "debug", False):
        import logging
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr,
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")
        logging.getLogger("meshcore").setLevel(logging.DEBUG)
    if getattr(args, "max_bytes", None) == 0:
        args.max_bytes = None
    try:
        if not inspect.iscoroutinefunction(args.fn):   # menu, setup, update: ask questions
            raise SystemExit(args.fn(args))
        raise SystemExit(asyncio.run(args.fn(args)))
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
