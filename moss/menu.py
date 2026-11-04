"""`moss` with no arguments: a menu for everything, so nobody has to
remember flags. Every item runs the same code as the matching command
(listed in the README), so scripts and the menu never disagree.

Arrow keys to move, Enter to pick, Ctrl+C to go back.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from . import config, pages as PG, radiocheck, service, setup, ui, updater


# ---------------------------------------------------------------- helpers

def run_cli(argv: list[str], wait: bool = True) -> None:
    """Run a normal `moss ...` command from inside the menu."""
    from .cli import build_parser
    from .pages_cli import fix_args
    try:
        args = build_parser().parse_args(argv)
        fix_args(args)
        asyncio.run(args.fn(args))
    except KeyboardInterrupt:
        ui.note("stopped")
    except SystemExit as exc:
        if exc.code not in (None, 0):
            ui.fail(str(exc.code))
    if wait:
        ui.pause()


def _run_async(coro) -> None:
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        ui.note("stopped")
    except (Exception, SystemExit) as exc:
        ui.fail(str(exc))


def _back(label: str = "Back") -> ui.Choice:
    return ui.Choice(f"← {label}", "__back")


def _loop(title_fn, items_fn) -> None:
    """Show a menu until Back/Ctrl+C. items_fn() -> [(label, action)]."""
    while True:
        title_fn()
        items = items_fn()
        choices = [ui.Choice(label, i) for i, (label, _) in enumerate(items)]
        try:
            pick = ui.choose("What would you like to do?", choices + [_back()])
        except ui.Back:
            return
        if pick == "__back":
            return
        try:
            items[pick][1]()
        except ui.Back:
            pass


# ---------------------------------------------------------------- identities and pages

def new_identity() -> PG.Identity | None:
    authors = PG.Authors()
    name = ui.text("Display name for your pages (anyone can see it):")
    if not name:
        return None
    try:
        ident = authors.create(name)
    except PG.PageError as exc:
        ui.fail(str(exc))
        return None
    ui.ok(f"made {ident.name!r}, fingerprint {ident.fingerprint}")
    ui.note("Without this key you can never update or delete pages you publish with it.")
    if ui.confirm("Back it up now (a passphrase-protected file)?", True):
        backup_identity(ident)
    return ident


def backup_identity(ident: PG.Identity) -> None:
    default = str(Path.home() / f"moss-identity-{PG.slugify(ident.label())}.json")
    where = ui.path("Save the backup where?", default)
    while True:
        p1 = ui.secret("Passphrase (a few random words):")
        if p1 != ui.secret("Same passphrase again:"):
            ui.warn("they didn't match; again")
            continue
        try:
            PG.Authors().backup(ident, Path(where).expanduser(), p1)
        except PG.PageError as exc:
            ui.warn(str(exc))
            continue
        ui.ok(f"backed up to {where}. Keep the file and the passphrase in different places.")
        return


def _pick_identity(question: str, allow_anonymous_new: bool = False):
    authors = PG.Authors()
    named = [i for i in authors.all() if not i.anonymous]
    if not named and not allow_anonymous_new:
        ui.warn("no identities yet")
        return None
    default = authors.default()
    choices = [ui.Choice(f"{i.label()}  ({i.fingerprint})", i.id) for i in named]
    if allow_anonymous_new:
        choices.append(ui.Choice("A new anonymous identity (nothing links it to you)", "__anon"))
    pick = ui.choose(question, choices, default=default.id if default else None)
    return pick if pick == "__anon" else authors.find(pick)


def _all_pages(include_deleted: bool = False) -> list[tuple[PG.Identity, str, dict]]:
    out = []
    for ident in PG.Authors().all():
        for name, info in sorted(ident.published().items()):
            if include_deleted or not info["deleted"]:
                out.append((ident, name, info))
    return out


def _pick_page(question: str):
    pages = _all_pages()
    if not pages:
        ui.warn("you haven't published any pages yet")
        return None
    choices = [ui.Choice(f"{info['title'] or name}  ·  v{info['version']} {info['visibility']}  ·  "
                         f"{ident.label()}", (ident.id, name)) for ident, name, info in pages]
    aid, name = ui.choose(question, choices)
    return PG.Authors().find(aid), name


def _md_file(question: str = "Which Markdown (.md) file?") -> tuple[str, str]:
    while True:
        p = Path(ui.path(question)).expanduser()
        try:
            return str(p), p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            ui.warn(f"can't read {p}: {exc}")


def _finish(page: PG.Page) -> None:
    path = PG.save_to_outbox(page)
    ui.ok(f"signed: v{page.version}, {page.spores} spore{'s' if page.spores != 1 else ''}")
    ui.console.print(f"\n  [bold]{page.address}[/]\n")
    if page.spores > PG.DEFAULT_CAP and not page.deleted:
        ui.warn(f"over the usual {PG.DEFAULT_CAP}-spore cap: a vault will refuse it unless its "
                f"owner raised the cap or approves it from the review queue")
    ui.note(f"saved in the outbox: {path}")
    if ui.confirm("Upload it to the Seed Vault now?", True):
        run_cli(["page", "upload", page.address])
    else:
        ui.note("Upload it later from My pages → Upload to the Seed Vault.")


def new_page() -> None:
    authors = PG.Authors()
    if not [i for i in authors.all() if not i.anonymous]:
        ui.note("First, an identity for your pages.")
        if new_identity() is None:
            return
    path, md = _md_file()
    vis = ui.choose("Who can find it?", [
        ui.Choice("Public: anyone can find it in search", PG.PUBLIC),
        ui.Choice("Unlisted: only people you give the link to", PG.UNLISTED)])
    who = _pick_identity("Post as?", allow_anonymous_new=True)
    title = PG._first_heading(md) or Path(path).stem
    if vis == PG.PUBLIC:
        name = ui.text("Name in its address:", PG.slugify(title),
                       validate=lambda s: bool(PG.NAME_RULE.match(s)) or
                       "lowercase letters, digits and dashes, like water-filters")
    else:
        name = PG.random_name()
        if who != "__anon":
            ui.warn("this link names you, so whoever gets it can look up your public pages. "
                    "An anonymous identity avoids that.")
    if who != "__anon" and name in who.published():
        ui.fail(f"you already have a page called {name}; use Update instead")
        return
    ui.console.print(f"\n  [bold]{title}[/]  ·  {vis}  ·  "
                     f"{'a new anonymous identity' if who == '__anon' else who.label()}\n")
    if not ui.confirm("Sign it?", True):
        return
    if who == "__anon":
        who = authors.create(anonymous=True, note=name)
    try:
        _finish(who.sign(name, md, visibility=vis))
    except PG.PageError as exc:
        ui.fail(str(exc))


def update_page() -> None:
    picked = _pick_page("Update which page?")
    if not picked:
        return
    ident, name = picked
    _, md = _md_file("Which file has the new text?")
    try:
        _finish(ident.sign(name, md, visibility=ident.published()[name]["visibility"]))
    except PG.PageError as exc:
        ui.fail(str(exc))


def restore_page() -> None:
    picked = _pick_page("Restore an old version of which page?")
    if not picked:
        return
    ident, name = picked
    versions = ident.history(name)
    if not versions:
        ui.warn("no older versions saved on this machine")
        return
    v = ui.choose("Bring back which version? (It's published again as a new version.)",
                  [ui.Choice(f"v{n}", n) for n in reversed(versions)])
    preview = ident.old_text(name, v).strip().splitlines()[:6]
    ui.note("\n".join(preview))
    if ui.confirm(f"Publish v{v}'s text as the newest version?", True):
        _finish(ident.sign(name, ident.old_text(name, v),
                           visibility=ident.published()[name]["visibility"]))


def delete_page() -> None:
    picked = _pick_page("Delete which page?")
    if not picked:
        return
    ident, name = picked
    if ui.confirm(f"Delete {name}? Vaults will replace it with a signed 'deleted' notice.", False):
        _finish(ident.sign(name, None))


def list_pages() -> None:
    pages = _all_pages(include_deleted=True)
    if not pages:
        ui.warn("you haven't published any pages yet")
    else:
        t = ui.Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        for col in ("Title", "Version", "Who can find it", "Identity"):
            t.add_column(col)
        t.add_column("Address", overflow="fold")
        for ident, name, info in pages:
            state = "deleted" if info["deleted"] else info["visibility"]
            t.add_row(info["title"] or name, f"v{info['version']}", state, ident.label(),
                      f"moss:{ident.id}/{name}")
        ui.console.print(t)
    ui.pause()


def check_page_file() -> None:
    p = Path(ui.path("Which .mosspage file?", str(PG.outbox()) + "/")).expanduser()
    try:
        page = PG.load(p.read_bytes())
    except (OSError, PG.PageError) as exc:
        ui.fail(str(exc))
    else:
        ui.ok("signature OK")
        ui.console.print(page.describe())
    ui.pause()


def upload_pages() -> None:
    pending = PG.outbox_pending()
    if not pending:
        ui.warn("nothing waiting to upload")
        ui.pause()
        return
    choices = [ui.Choice(f"Everything waiting ({len(pending)} page{'s' if len(pending) != 1 else ''})", "__all")]
    choices += [ui.Choice(f"{p.record['title'] or 'deletion'}  ·  v{p.version}  ·  {p.spores} spores",
                          p.address) for _, p in pending]
    pick = ui.choose("Upload which?", choices)
    run_cli(["page", "upload"] + ([] if pick == "__all" else [pick]))


def open_by_address() -> None:
    address = ui.text("Page address (moss:...):",
                      validate=lambda s: s.strip().startswith("moss:") or "addresses start with moss:")
    run_cli(["page", "open", address.strip()])


def pages_menu() -> None:
    def head():
        n = len(PG.outbox_pending())
        ui.header("My pages", "Pages you write, signed with your identity.",
                  f"{n} waiting to upload" if n else "nothing waiting to upload")

    _loop(head,
          lambda: [("New page", new_page), ("Upload to the Seed Vault", upload_pages),
                   ("Update a page", update_page),
                   ("Restore an old version", restore_page), ("Delete a page", delete_page),
                   ("List my pages", list_pages), ("Check a page file", check_page_file)])


def identities_menu() -> None:
    def show():
        authors = PG.Authors()
        d = authors.default()
        lines = [f"{'*' if d and d.id == i.id else ' '} {i.label():28} {i.fingerprint}  "
                 f"{len(i.published())} page(s)" for i in authors.all()]
        ui.header("Identities", *(lines or ["none yet"]), "", "* = used for new pages")

    def make_default():
        ident = _pick_identity("Use which identity for new pages?")
        if ident:
            PG.Authors().set_default(ident)
            ui.ok(f"new pages now come from {ident.label()}")

    def rename():
        ident = _pick_identity("Rename which identity?")
        if ident:
            new = ui.text("New display name:", ident.name)
            if new and len(new) <= PG.LIMITS["author"]:
                ident.meta["name"] = new
                ident._save_meta()
                ui.ok("renamed; pages you publish from now on carry the new name")

    def backup():
        ident = _pick_identity("Back up which identity?")
        if ident:
            backup_identity(ident)

    def restore():
        f = ui.path("Backup file:")
        try:
            ident = PG.Authors().restore(Path(f).expanduser(), ui.secret("Its passphrase:"))
            ui.ok(f"restored {ident.label()} ({ident.fingerprint})")
        except PG.PageError as exc:
            ui.fail(str(exc))

    _loop(show, lambda: [("New identity", new_identity), ("Choose the default", make_default),
                         ("Rename", rename), ("Back up", backup), ("Restore from a backup", restore)])


# ---------------------------------------------------------------- radio check

def radio_menu() -> None:
    port = None                 # whatever setup saved: a USB port or a Bluetooth radio

    def send():
        n = int(ui.text("How many pings?", "10", validate=lambda s: s.isdigit() and 0 < int(s) <= 100))
        _run_async(radiocheck.send(port, n, out=ui.console.print))
        ui.pause()

    def listen():
        secs = float(ui.text("Listen for how many seconds?", "300",
                             validate=lambda s: s.isdigit() and int(s) > 0))
        _run_async(radiocheck.listen(port, secs, out=ui.console.print))
        ui.pause()

    def info():
        _run_async(radiocheck.info(port, out=ui.console.print))
        ui.pause()

    def dedicated():
        from . import radiocheck as RC
        ui.note("A dedicated link puts MOSS radios on their own frequency, off the public mesh: "
                "no load on other people's repeaters, but these radios then can't hear the public "
                "mesh either. Every MOSS radio (vault and readers) needs the same settings.")
        pick = ui.choose("Which settings?", [
            ui.Choice("Back to the public mesh (MeshCore US: 910.525 MHz, 62.5 kHz, SF7, CR5)", "public"),
            ui.Choice("Custom: enter frequency, bandwidth, spreading factor, coding rate", "custom")])
        if pick == "public":
            settings = RC.PUBLIC_US
        else:
            try:
                settings = (float(ui.text("Frequency (MHz):", "915.0")), float(ui.text("Bandwidth (kHz):", "62.5")),
                            int(ui.text("Spreading factor (5-12):", "7")), int(ui.text("Coding rate (5-8):", "5")))
            except ValueError:
                ui.fail("those need to be numbers")
                return
        for problem in RC.check_link(*settings):
            ui.warn(problem)
        if ui.confirm(f"Set this radio to {settings[0]:g} MHz, {settings[1]:g} kHz, SF{settings[2]}, "
                      f"CR{settings[3]}?", False):
            _run_async(RC.set_link(None, *settings, out=ui.console.print))
        ui.pause()

    _loop(lambda: ui.header("Radio check",
                            "Send numbered pings on #moss from one radio and listen on the other. "
                            "They're short chat messages anyone on #moss can see.",
                            *(["The Seed Vault pauses during the check and starts again after."]
                              if config.load().get("role") == "vault" else [])),
          lambda: [("Listen for pings", listen), ("Send pings", send),
                   ("Show this radio's settings", info),
                   ("Dedicated link (own frequency, off the public mesh)", dedicated)]
          + ([] if config.load().get("role") == "vault" else
             [("Full test with the Seed Vault (search, upload, read back)",
               lambda: run_cli(["selftest"]))]))


# ---------------------------------------------------------------- shared items

def do_update() -> None:
    cfg = config.load()
    branch = ui.choose("Update from which branch?", [
        ui.Choice("main          stable", "main"),
        ui.Choice("experimental  newest, for testing", "experimental")],
        default=cfg.get("branch") or "main")
    try:
        updater.update(branch, out=ui.console.print)
    except updater.UpdateError as exc:
        ui.fail(str(exc))
    else:
        ui.note("Changes take effect the next time you start moss.")
    ui.pause()


def _vault_fp() -> str:
    from .seal import Keyring
    ring = Keyring()
    return ring.fingerprint or "not met yet"


# ---------------------------------------------------------------- reader

def reader_menu() -> None:
    def head():
        cfg = config.load()
        ui.header("MOSS · reader",
                  f"radio   {config.radio_label(cfg)}",
                  f"vault   {_vault_fp()}",
                  f"version {updater.current()}")

    def saved():
        from .cache import Cache
        items = Cache().list_articles()
        if not items:
            ui.warn("nothing saved yet")
            ui.pause()
            return
        title = ui.choose("Read which?", [ui.Choice(a.title, a.title) for a in items])
        run_cli(["cache", "show", title])

    def search():
        q = ui.text("Search for:")
        if q:
            run_cli(["search", *q.split()])

    def settings():
        def port():
            link = setup._pick_link(ui, config.load())
            if link is None:
                config.update(port=None, ble=None)
                ui.ok("no radio set")
            elif setup._check_radio(ui, link) or setup._retry_radio(ui, link):
                setup._save_reader(ui, link, False)

        def scope():
            s = ui.text("MeshCore region to keep traffic inside (blank for none):",
                        config.load().get("scope") or "")
            config.update(scope=s or None)

        def trust():
            from .seal import Keyring
            ring = Keyring()
            ui.note(f"trusting {ring.data.get('name', 'Seed Vault')} {ring.fingerprint}"
                    if ring.fingerprint else "no Seed Vault trusted yet")
            if ring.fingerprint and ui.confirm("Forget it, so the next vault heard is trusted?", False):
                ring.forget()
                ui.ok("forgotten")

        def routing():
            on = config.load().get("route")
            ui.note("Routing sends your uploads back along the route the Seed Vault's messages came "
                    "by, so only the repeaters in between carry them instead of every repeater. "
                    "It falls back to flooding whenever no route is known. (The vault decides "
                    "for what it sends, with its own --route option.)")
            if ui.confirm(f"Routing is {'on' if on else 'off'}. Turn it {'off' if on else 'on'}?", True):
                config.update(route=not on)
                ui.ok(f"routing {'off' if on else 'on'}")

        _loop(lambda: None, lambda: [("Radio (USB or Bluetooth)", port), ("Region scope", scope),
                                     ("Routing instead of flooding (opt-in)", routing),
                                     ("Trusted Seed Vault", trust),
                                     ("Run the whole setup again", lambda: setup.run())])

    _loop(head, lambda: [
        ("Open the reader in your browser", lambda: run_cli(["web"], wait=False)),
        ("Search from here", search),
        ("Open a page by address", open_by_address),
        ("This Seed Vault (version, library, free storage, spore print)", lambda: run_cli(["vault-info"])),
        ("Read saved articles", saved),
        ("My pages", pages_menu),
        ("Identities", identities_menu),
        ("Radio check", radio_menu),
        ("Settings", settings),
        ("Update MOSS", do_update),
    ])


# ---------------------------------------------------------------- Seed Vault

def vault_menu() -> None:
    def head():
        env = service.read_env()
        state = ("[green]● running[/] since " + service.since()) if service.is_active() \
            else "[red]● stopped[/]"
        keys_dir = config.moss_home() / "seed-vault"
        fp = "?"
        if (keys_dir / "ed25519.pem").exists():
            from .seal import VaultKeys
            fp = VaultKeys.load_or_create(keys_dir).fingerprint
        ui.header("MOSS · Seed Vault", state,
                  f"library {env.get('MOSS_ZIM', 'not set')}",
                  f"radio   {env.get('MOSS_PORT', 'not set')}",
                  f"keys    {fp}",
                  f"version {updater.current()}")

    def status():
        ui.console.print(service.recent_log(20) or "(no log yet)")
        ui.pause()

    def control():
        action = ui.choose("Start, stop or restart?", [
            ui.Choice("Restart", "restart"), ui.Choice("Stop", "stop"), ui.Choice("Start", "start")])
        if action == "stop":
            ui.note("stopped until you start it again (it still starts on boot)")
        (ui.ok if service.control(action) else ui.fail)(f"{action}: done")

    def _resave(port=None, zim=None, extra=None):
        env = service.read_env()
        ok = service.save_settings(port or env.get("MOSS_PORT", ""), zim or env.get("MOSS_ZIM", ""),
                                   env.get("MOSS_EXTRA", "") if extra is None else extra)
        if ok and service.control("restart"):
            ui.ok("saved, Seed Vault restarted")
        else:
            ui.fail("couldn't save the settings")

    def library():
        _resave(zim=setup._pick_zim(ui, service.read_env().get("MOSS_ZIM")))

    def port():
        p = setup._pick_port(ui, service.read_env().get("MOSS_PORT"), "vault")
        config.update(port=p)
        _resave(port=p)

    def extra():
        ui.note("Extra options for the gateway, e.g.  --gap 0.5  --pad-sizes  --scope NAME  "
                "(see docs/seed-vault.md → Seed Vault options)")
        e = ui.text("Extra options:", service.read_env().get("MOSS_EXTRA", ""))
        if re.search(r"[;&|`$<>]", e):
            ui.fail("options only, please (no shell symbols)")
            return
        _resave(extra=e)

    def spore_print():
        from . import sporeprint as SP
        vp = SP.VaultPrint(config.moss_home() / "seed-vault")
        sp = vp.get()
        if sp:
            ui.ok(SP.describe(SP.summary(sp)))
            ui.note("A spore print never changes once a vault has one.")
        else:
            ui.note("No spore print yet. One arrives the first time a reader carrying one visits, "
                    "or this vault can start an original family tree.")
            if ui.confirm("Start an original spore print here? (Only the first vault of a family "
                          "tree should; it can't be undone.)", False):
                note = ui.text("Note for the original (public):",
                               "The original Seed Vault, ATLAS, CU Boulder, Colorado")
                run_cli(["spore-print", note], wait=False)
                if service.is_active():
                    service.control("restart")
        ui.pause()

    _loop(head, lambda: [
        ("Recent log", status),
        ("Watch the log live", service.follow_log),
        ("Start / stop / restart", control),
        ("Radio check", radio_menu),
        ("Change library", library),
        ("Change radio port", port),
        ("Extra options", extra),
        ("Spore print", spore_print),
        ("Update MOSS", do_update),
        ("Run the whole setup again", lambda: setup.run()),
    ])


def main() -> int:
    if not ui.interactive():
        print("the menu needs a terminal; see `moss --help` for commands")
        return 1
    if config.load().get("role") is None:
        ui.note("This machine isn't set up yet.")
        try:
            if setup.run() != 0:
                return 1
        except ui.Back:
            return 1
    try:
        (vault_menu if config.load().get("role") == "vault" else reader_menu)()
    except (ui.Back, KeyboardInterrupt):
        pass
    return 0
