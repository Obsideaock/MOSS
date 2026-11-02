"""`moss author ...` and `moss page ...`: community pages from the command line.

    python -m moss author new "Stern"                 # your first identity (becomes the default)
    python -m moss author list
    python -m moss author backup Stern stern-key.json   # passphrase-protected copy of the key

    python -m moss page make water-filters.md                       # public, as your default identity
    python -m moss page make clinic-hours.md --unlisted --anonymous # link-only, from a fresh identity
    python -m moss page update moss:<id>/water-filters water-filters.md
    python -m moss page restore moss:<id>/water-filters 2           # old text back, as a new version
    python -m moss page delete moss:<id>/water-filters
    python -m moss page check ~/.moss/outbox/<file>.mosspage
    python -m moss page mine

    python -m moss page upload                                      # everything in the outbox
    python -m moss page upload moss:<id>/water-filters              # just that page
    python -m moss page open moss:<id>/water-filters                # read a page by its address

Finished pages wait in ~/.moss/outbox/ until uploaded; sent ones move to
~/.moss/outbox/sent/.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

from . import pages as PG


def _authors() -> PG.Authors:
    return PG.Authors()


def _fail(exc: Exception) -> int:
    print(f"error: {exc}", file=sys.stderr)
    return 1


def _new_passphrase() -> str:
    first = getpass.getpass("passphrase for the backup (a few random words): ")
    if first != getpass.getpass("same passphrase again: "):
        raise PG.PageError("the two passphrases didn't match")
    return first


def _who(a: PG.Authors, args) -> PG.Identity:
    if getattr(args, "as_", None):
        return a.find(args.as_)
    ident = a.default()
    if ident is None:
        raise PG.PageError('no author identity yet; make one first: moss author new "Your Name"')
    return ident


def _report(page: PG.Page, path: Path) -> None:
    print(page.describe())
    if page.spores > PG.DEFAULT_CAP and not page.deleted:
        print(f"  note: that's over the usual {PG.DEFAULT_CAP}-spore cap. A vault will refuse it "
              f"unless its owner raised the cap or approves it from the review queue.")
    print(f"  saved for upload: {path}")


# ---------------------------------------------------------------- moss author

async def cmd_author(args) -> int:
    a = _authors()
    try:
        if args.action == "new":
            name = " ".join(args.words)
            ident = a.create(name)
            print(f"made identity {ident.name!r}: fingerprint {ident.fingerprint}, id {ident.id}")
            if (a.default() or ident).id == ident.id:
                print("it's your default identity")
            print(f"back it up (losing it means you can't update its pages): "
                  f"moss author backup {ident.id[:8]} FILE")
        elif args.action == "list":
            idents = a.all()
            if not idents:
                print('no identities yet. Make one: moss author new "Your Name"')
            d = a.default()
            for i in idents:
                star = "*" if d and d.id == i.id else " "
                pages = i.published()
                print(f"{star} {i.label():30} {i.fingerprint}  {i.id}  "
                      f"{len(pages)} page{'s' if len(pages) != 1 else ''}")
        elif args.action == "use":
            ident = a.find(" ".join(args.words))
            a.set_default(ident)
            print(f"default identity is now {ident.name!r} ({ident.fingerprint})")
        elif args.action == "rename":
            ident = a.find(args.words[0])
            if ident.anonymous:
                raise PG.PageError("anonymous identities have no name")
            new = " ".join(" ".join(args.words[1:]).split())
            if not new or len(new) > PG.LIMITS["author"]:
                raise PG.PageError(f"give a new name of 1-{PG.LIMITS['author']} characters")
            ident.meta["name"] = new
            ident._save_meta()
            print(f"renamed to {new!r}; pages you publish from now on carry the new name")
        elif args.action == "backup":
            if len(args.words) != 2:
                raise PG.PageError("usage: moss author backup IDENTITY FILE")
            ident = a.find(args.words[0])
            a.backup(ident, Path(args.words[1]).expanduser(), _new_passphrase())
            print(f"backed up {ident.label()} to {args.words[1]}. Keep the file and the "
                  f"passphrase in different places.")
        elif args.action == "restore":
            ident = a.restore(Path(" ".join(args.words)).expanduser(),
                              getpass.getpass("backup passphrase: "))
            print(f"restored {ident.label()} ({ident.fingerprint})")
    except PG.PageError as exc:
        return _fail(exc)
    return 0


# ---------------------------------------------------------------- moss page

def _read_md(path: str) -> str:
    try:
        return Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise PG.PageError(f"can't read {path}: {exc.strerror}") from exc


async def cmd_page(args) -> int:
    a = _authors()
    try:
        if args.action == "make":
            md = _read_md(args.file)
            vis = PG.UNLISTED if args.unlisted else PG.PUBLIC
            if args.anonymous and args.as_:
                raise PG.PageError("pick one: --as or --anonymous")
            if args.name:
                name = args.name
            elif vis == PG.UNLISTED:
                name = PG.random_name()
            else:
                name = PG.slugify(PG._first_heading(md) or Path(args.file).stem)
            if args.anonymous:
                ident = a.create(anonymous=True, note=name)
            else:
                ident = _who(a, args)
            if name in ident.published():
                raise PG.PageError(f"{ident.label()} already published {name}; to change it: "
                                   f"moss page update moss:{ident.id}/{name} {args.file}")
            page = ident.sign(name, md, title=args.title, about=args.about, visibility=vis)
            _report(page, PG.save_to_outbox(page))
            if vis == PG.UNLISTED and not args.anonymous:
                print(f"  heads up: this link names you ({ident.label()}), so whoever gets it can "
                      f"look up your public pages. --anonymous posts from a fresh identity instead.")
        elif args.action in ("update", "restore", "delete"):
            ident = a.owner_of(args.address)
            _, name = PG.parse_address(args.address)
            prev = ident.published().get(name)
            if not prev:
                raise PG.PageError(f"nothing published as {args.address} from this device")
            if args.action == "update":
                if not args.extra:
                    raise PG.PageError("usage: moss page update ADDRESS FILE.md")
                page = ident.sign(name, _read_md(args.extra), title=args.title, about=args.about,
                                  visibility=prev["visibility"])
            elif args.action == "restore":
                if not args.extra or not args.extra.isdigit():
                    raise PG.PageError(f"usage: moss page restore ADDRESS VERSION "
                                       f"(saved: {ident.history(name)})")
                old = ident.old_text(name, int(args.extra))
                page = ident.sign(name, old, visibility=prev["visibility"])
                print(f"v{args.extra}'s text republished as v{page.version}")
            else:
                page = ident.sign(name, None)
            _report(page, PG.save_to_outbox(page))
        elif args.action == "check":
            page = PG.load(Path(args.address).expanduser().read_bytes())
            print("signature OK")
            print(page.describe())
            if args.show and not page.deleted:
                print()
                print(page.text())
        elif args.action == "mine":
            any_ = False
            for ident in a.all():
                for name, info in sorted(ident.published().items()):
                    any_ = True
                    state = "deleted" if info["deleted"] else info["visibility"]
                    print(f"{ident.label():24} v{info['version']:<3} {state:9} "
                          f"moss:{ident.id}/{name}  {info['title']}")
            if not any_:
                print("you haven't made any pages yet: moss page make FILE.md")
        elif args.action == "upload":
            return await _upload(args)
        elif args.action == "open":
            return await _open(args)
        elif args.action == "history":
            ident = a.owner_of(args.address)
            _, name = PG.parse_address(args.address)
            print(f"saved versions of {name}: {ident.history(name) or 'none'}")
    except (PG.PageError, OSError) as exc:
        return _fail(exc)
    return 0


async def _upload(args) -> int:
    from .cli import TermProgress, connect
    from .client import MossError
    pending = PG.outbox_pending()
    target = args.address
    if target and Path(target).expanduser().is_file():
        page = PG.load(Path(target).expanduser().read_bytes())
        pending = [(Path(target).expanduser(), page)]
    elif target:
        PG.parse_address(target)
        pending = [(f, p) for f, p in pending if p.address == target]
        if not pending:
            raise PG.PageError(f"nothing waiting in the outbox for {target}")
    if not pending:
        print("the outbox is empty: nothing to upload (make a page first: moss page make FILE.md)")
        return 0
    failed = 0
    async with connect(args) as (client, info):
        for f, page in pending:
            title = page.record["title"] or ("deletion" if page.deleted else page.address)
            print(f"uploading {title!r} v{page.version} ({page.spores} spores)...")
            show = TermProgress()
            try:
                message = await client.upload(f.read_bytes(), on_progress=show)
            except MossError as exc:
                show.finish()
                failed += 1
                print(f"  not uploaded: {exc}")
                continue
            show.finish()
            PG.mark_sent(page)
            print(f"  {message}: {page.address}")
    return 1 if failed else 0


async def _open(args) -> int:
    from .cli import TermProgress, connect, show_article
    from .client import MossError
    async with connect(args) as (client, info):
        show = TermProgress()
        try:
            got = await client.page(args.address, refresh=args.refresh, on_progress=show)
        except MossError as exc:
            show.finish()
            print(f"error: {exc}")
            return 1
        show.finish()
    page = got.page
    print(f"{page.describe()}\n  signature checked{' (saved copy)' if got.from_cache else ''}\n")
    if page.deleted:
        print("This page was deleted by its author.")
    else:
        show_article(page.text(), not args.no_pager)
    return 0


def add_parsers(sub, client_parent=None) -> None:
    au = sub.add_parser("author", help="your author identities for community pages")
    au.add_argument("action", choices=["list", "new", "use", "rename", "backup", "restore"],
                    nargs="?", default="list")
    au.add_argument("words", nargs="*")
    au.set_defaults(fn=cmd_author)

    pg = sub.add_parser("page", parents=[client_parent] if client_parent else [],
                        help="make, upload, open, update, restore, delete or check community pages")
    pg.add_argument("action", choices=["make", "update", "restore", "delete", "check", "mine",
                                       "history", "upload", "open"])
    pg.add_argument("address", nargs="?", metavar="FILE_OR_ADDRESS",
                    help="a .md file (make), a page address, or a .mosspage file (check)")
    pg.add_argument("extra", nargs="?", metavar="FILE_OR_VERSION")
    pg.add_argument("--name", help="page name in its address (default: from the title; random if unlisted)")
    pg.add_argument("--title", help="title (default: the file's first '# ' heading)")
    pg.add_argument("--about", help="one-line description shown in search (default: the first sentence)")
    pg.add_argument("--unlisted", action="store_true", help="link-only: never in search or listings")
    pg.add_argument("--anonymous", action="store_true",
                    help="post from a fresh identity that nothing links to your others")
    pg.add_argument("--as", dest="as_", metavar="IDENTITY", help="post as this identity, not the default")
    pg.add_argument("--show", action="store_true", help="check: also print the page text")
    pg.set_defaults(fn=cmd_page)


def fix_args(args) -> None:
    """`page make FILE`: the file arrives in the address slot."""
    if getattr(args, "cmd", None) != "page":
        return
    if getattr(args, "fn", None) is cmd_page and args.action == "make":
        args.file = args.address
        if not args.file:
            raise SystemExit("usage: moss page make FILE.md")
    if getattr(args, "fn", None) is cmd_page and args.action not in ("make", "mine", "upload") \
            and not args.address:
        raise SystemExit(f"usage: moss page {args.action} ADDRESS")
