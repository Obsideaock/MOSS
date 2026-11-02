"""A small local web reader on top of MossClient.

    python -m moss web --port COM12 --channel 1

Opens http://127.0.0.1:8765. Nothing here needs the internet: the page uses
system fonts and no external scripts, because the machine running it is
often the one that's off-grid.

The browser starts a job (search or article) and polls it. The job's
progress carries the packet map and the text decoded so far, which is what
the page animates while the radio does its slow work.

Safety: this controls a radio, so it binds to localhost only, rejects
requests whose Host header isn't localhost (DNS rebinding), and requires
JSON bodies on POST so another website can't fire requests at it.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import pages as PG
from .client import MossClient, MossError, Progress

INDEX = Path(__file__).with_name("index.html")
STATIC = Path(__file__).with_name("static")          # bundled KaTeX, so math works offline
STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".woff2": "font/woff2"}
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class Job:
    def __init__(self, kind: str, query: str, params: dict | None = None):
        self.id = secrets.token_hex(6)
        self.kind, self.query = kind, query
        self.params = params or {}
        self.state = "running"          # running, done, error
        self.progress: dict = Progress(stage="waiting",
                                       message="waiting for the radio").to_dict()
        self.result: dict | None = None
        self.error: str | None = None
        self.created = time.time()

    def to_dict(self) -> dict:
        d = {"id": self.id, "kind": self.kind, "query": self.query,
             "state": self.state, "progress": self.progress}
        if self.result is not None:
            d["result"] = self.result
        if self.error:
            d["error"] = self.error
        return d


class App:
    def __init__(self, client: MossClient, loop: asyncio.AbstractEventLoop,
                 info: dict):
        self.client, self.loop, self.info = client, loop, info
        self.jobs: dict[str, Job] = {}

    # ---- runs on the asyncio loop
    async def _run(self, job: Job, refresh: bool) -> None:
        def on_progress(p) -> None:
            job.progress = p.to_dict()
        try:
            if job.kind == "search":
                out = await self.client.search(job.query, refresh, on_progress)
                job.result = {
                    "query": out.query, "from_cache": out.from_cache,
                    "fetched_at": out.fetched_at,
                    "results": [{"title": r.title, "blurb": r.blurb}
                                for r in out.results],
                    "community": self._marked(out.community), "more": out.more}
            elif job.kind == "more":
                found, more = await self.client.more_community(job.query, int(job.params.get("offset", 0)),
                                                               on_progress)
                job.result = {"community": self._marked(found), "more": more}
            elif job.kind == "author":
                found, more = await self.client.author_pages(job.query, 0, on_progress)
                job.result = {"author_id": job.query, "community": self._marked(found), "more": more,
                              "name": job.params.get("name", ""),
                              "following": self.client.social.is_following(job.query)}
            elif job.kind == "specs":
                specs = await self.client.specs(on_progress)
                from . import sporeprint as SP
                mine = self.client.prints.mine()
                specs["mine"] = SP.describe(SP.summary(mine)) if mine else ""
                from . import PROTOCOL
                specs["older"] = int(specs.get("protocol", 0)) < PROTOCOL
                if specs.get("spore_print"):
                    specs["spore_print_text"] = SP.describe(specs["spore_print"])
                job.result = specs
            elif job.kind == "upload":
                pending = [(f, p) for f, p in PG.outbox_pending() if p.address == job.query]
                if not pending:
                    raise MossError("nothing waiting to upload for that page")
                f, page = pending[0]
                message = await self.client.upload(f.read_bytes(), on_progress)
                PG.mark_sent(page)
                job.result = {"message": message, "address": page.address, "version": page.version}
            elif job.kind == "page":
                out = await self.client.page(job.query, refresh, on_progress)
                pg, r = out.page, out.page.record
                self.client.social.note_opened(pg.address)
                aid = PG.parse_address(pg.address)[0]
                job.result = {
                    "title": r["title"] or "Deleted page", "text": "" if pg.deleted else pg.text(),
                    "from_cache": out.from_cache, "fetched_at": out.fetched_at,
                    "elapsed": job.progress.get("elapsed", 0),
                    "packets": job.progress.get("total", 0),
                    "page": {"address": pg.address, "author": r["author"] or "anonymous",
                             "fingerprint": pg.fingerprint, "version": pg.version,
                             "visibility": r["visibility"], "deleted": pg.deleted,
                             "signed": r["time"], "about": r["about"], "author_id": aid,
                             "following": self.client.social.is_following(aid),
                             "favourite": pg.address in self.client.social.favourites,
                             "mine": _is_mine(pg.address)}}
            else:
                out = await self.client.article(job.query, refresh, on_progress)
                job.result = {
                    "title": out.title, "text": out.text,
                    "from_cache": out.from_cache, "fetched_at": out.fetched_at,
                    "elapsed": job.progress.get("elapsed", 0),
                    "packets": job.progress.get("total", 0)}
            job.state = "done"
        except MossError as exc:
            job.state, job.error = "error", str(exc)
        except Exception as exc:                       # never hang the page
            job.state, job.error = "error", f"unexpected: {exc!r}"

    def _marked(self, found: list[dict]) -> list[dict]:
        """Add what this reader knows locally (never sent anywhere)."""
        s = self.client.social
        return [dict(r, following=s.is_following(r["a"].split(":", 1)[1].split("/", 1)[0]),
                     favourite=r["a"] in s.favourites) for r in found]

    def community(self) -> dict:
        """The Community shelf: saved pages, favourites, follows, your own pages."""
        saved = []
        folder = Path(self.client.cache.folder) / "pages"
        for f in sorted(folder.glob("*/*.mosspage")) if folder.is_dir() else []:
            try:
                page = PG.load(f.read_bytes())
            except (OSError, PG.PageError):
                continue
            saved.append({"address": page.address, "title": page.record["title"] or "Deleted page",
                          "author": page.record["author"] or "anonymous", "deleted": page.deleted,
                          "fetched_at": f.stat().st_mtime})
        mine = []
        for ident in PG.Authors().all():
            for name, info in sorted(ident.published().items()):
                mine.append({"address": f"moss:{ident.id}/{name}", "title": info["title"] or name,
                             "version": info["version"], "visibility": info["visibility"],
                             "deleted": info["deleted"], "identity": ident.label()})
        waiting = {p.address for _, p in PG.outbox_pending()}
        for m in mine:
            m["waiting"] = m["address"] in waiting
        s = self.client.social
        return {"saved": saved, "favourites": s.favourites, "follows": s.follows, "mine": mine}

    def submit(self, kind: str, query: str, refresh: bool, params: dict | None = None) -> Job:
        # Forget old jobs so the table doesn't grow forever.
        for jid in [j for j, v in self.jobs.items()
                    if v.state != "running" and time.time() - v.created > 3600]:
            del self.jobs[jid]
        job = Job(kind, query, params)
        self.jobs[job.id] = job
        asyncio.run_coroutine_threadsafe(self._run(job, refresh), self.loop)
        return job

    def cached(self) -> list[dict]:
        return [{"title": a.title, "bytes": a.bytes, "fetched_at": a.fetched_at}
                for a in self.client.cached()]


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MOSS"

        def log_message(self, *args):        # keep the terminal for the radio log
            pass

        # ---- helpers
        def _host_ok(self) -> bool:
            host = self.headers.get("Host") or ""
            if host.startswith("["):                    # [::1]:8765
                name = host.split("]")[0] + "]"
            else:
                name = host.rsplit(":", 1)[0] if ":" in host else host
            return name in ALLOWED_HOSTS

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _static(self, rel: str) -> None:
            root = STATIC.resolve()
            path = (root / rel).resolve()
            ctype = STATIC_TYPES.get(path.suffix)
            if not ctype or root not in path.parents or not path.is_file():
                return self._json({"error": "not found"}, 404)
            self._send(200, path.read_bytes(), ctype)

        def _guard(self) -> bool:
            if not self._host_ok():
                self._json({"error": "bad host"}, 403)
                return False
            return True

        # ---- routes
        def do_GET(self):
            if not self._guard():
                return
            url = urlparse(self.path)
            if url.path == "/":
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            elif url.path.startswith("/static/"):
                self._static(url.path[len("/static/"):])
            elif url.path == "/api/info":
                tx = app.client.requester.tx
                ring = app.client.requester.keyring
                self._json({**app.info, "radio": getattr(tx, "state", "connected"),
                            "reconnects": getattr(tx, "reconnects", 0),
                            "encrypted": ring is not None,
                            "vault": ring.fingerprint if ring is not None else None})
            elif url.path == "/api/cached":
                self._json(app.cached())
            elif url.path == "/api/community":
                self._json(app.community())
            elif url.path == "/api/identities":
                self._json(_identities())
            elif url.path == "/api/mytext":
                address = (parse_qs(url.query).get("address") or [""])[0]
                try:
                    ident = PG.Authors().owner_of(address)
                    _, name = PG.parse_address(address)
                    v = ident.published()[name]["version"]
                    self._json({"text": ident.old_text(name, v), "address": address})
                except (PG.PageError, KeyError) as exc:
                    self._json({"error": str(exc)}, 404)
            elif url.path.startswith("/api/job/"):
                job = app.jobs.get(url.path.rsplit("/", 1)[-1])
                self._json(job.to_dict() if job else {"error": "no such job"},
                           200 if job else 404)
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            if not self._guard():
                return
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._json({"error": "JSON only"}, 415)
            path = urlparse(self.path).path
            limit = 1_000_000 if path == "/api/pages/sign" else 4096   # a whole page, or a request
            try:
                size = int(self.headers.get("Content-Length") or 0)
                if size > limit:
                    return self._json({"error": "too big"}, 413)
                body = json.loads(self.rfile.read(size) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except (ValueError, OSError):
                return self._json({"error": "bad body"}, 400)
            if path == "/api/search":
                q = str(body.get("q", "")).strip()
                if not q:
                    return self._json({"error": "type something to search for"}, 400)
                self._json(app.submit("search", q, bool(body.get("refresh"))).to_dict())
            elif path == "/api/page":
                a = str(body.get("address", "")).strip()
                if not a.startswith("moss:"):
                    return self._json({"error": "that isn't a page address (moss:...)"}, 400)
                self._json(app.submit("page", a, bool(body.get("refresh"))).to_dict())
            elif path == "/api/more":
                q = str(body.get("q", "")).strip()
                self._json(app.submit("more", q, False, {"offset": int(body.get("offset") or 0)}).to_dict())
            elif path == "/api/author":
                aid = str(body.get("id", "")).strip().lower()
                if len(aid) != 26:
                    return self._json({"error": "not an author id"}, 400)
                self._json(app.submit("author", aid, False, {"name": str(body.get("name", ""))[:60]}).to_dict())
            elif path == "/api/specs":
                self._json(app.submit("specs", "specs", False).to_dict())
            elif path == "/api/upload":
                self._json(app.submit("upload", str(body.get("address", "")), False).to_dict())
            elif path == "/api/follow":
                aid = str(body.get("author_id", "")).strip().lower()
                if body.get("on"):
                    app.client.social.follow(aid, str(body.get("name", ""))[:60])
                else:
                    app.client.social.unfollow(aid)
                self._json({"following": app.client.social.is_following(aid)})
            elif path == "/api/favourite":
                a = str(body.get("address", ""))
                app.client.social.favourite(a, str(body.get("title", ""))[:120], bool(body.get("on")))
                self._json({"favourite": a in app.client.social.favourites})
            elif path == "/api/identities":
                try:
                    ident = PG.Authors().create(str(body.get("name", "")))
                    self._json({"id": ident.id, "label": ident.label(), "fingerprint": ident.fingerprint})
                except PG.PageError as exc:
                    self._json({"error": str(exc)}, 400)
            elif path == "/api/pages/sign":
                try:
                    self._json(_sign_page(body))
                except PG.PageError as exc:
                    self._json({"error": str(exc)}, 400)
            elif path == "/api/article":
                t = str(body.get("title", "")).strip()
                if not t:
                    return self._json({"error": "no title"}, 400)
                self._json(app.submit("article", t, bool(body.get("refresh"))).to_dict())
            else:
                self._json({"error": "not found"}, 404)

        def do_DELETE(self):
            if not self._guard():
                return
            url = urlparse(self.path)
            if url.path == "/api/cached":
                title = (parse_qs(url.query).get("title") or [""])[0]
                self._json({"removed": app.client.cache.forget(title)})
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def _is_mine(address: str) -> bool:
    try:
        PG.Authors().owner_of(address)
        return True
    except PG.PageError:
        return False


def _identities() -> list[dict]:
    a = PG.Authors()
    d = a.default()
    return [{"id": i.id, "label": i.label(), "fingerprint": i.fingerprint, "anonymous": i.anonymous,
             "default": bool(d and d.id == i.id)} for i in a.all()]


def _sign_page(body: dict) -> dict:
    """From the editor: sign a new page or a new version, and put it in the outbox."""
    text = str(body.get("text", ""))
    if not text.strip():
        raise PG.PageError("the page is empty")
    authors = PG.Authors()
    title = (str(body.get("title", "")).strip() or None)
    if body.get("address"):                                     # a new version of one of mine
        ident = authors.owner_of(body["address"])
        _, name = PG.parse_address(body["address"])
        page = ident.sign(name, text, title=title, visibility=ident.published()[name]["visibility"])
    else:
        vis = PG.UNLISTED if body.get("visibility") == "unlisted" else PG.PUBLIC
        who = str(body.get("identity", ""))
        if vis == PG.UNLISTED:
            name = PG.random_name()
        else:
            name = str(body.get("name", "")).strip() or PG.slugify(title or PG._first_heading(text) or "page")
        if who == "anonymous":
            ident = authors.create(anonymous=True, note=name)
        else:
            ident = authors.find(who) if who else authors.default()
            if ident is None:
                raise PG.PageError("make an identity first")
        if name in ident.published():
            raise PG.PageError(f"you already have a page called {name}; edit that one instead")
        page = ident.sign(name, text, title=title, visibility=vis)
    PG.save_to_outbox(page)
    return {"address": page.address, "version": page.version, "spores": page.spores,
            "title": page.record["title"], "over_cap": page.spores > PG.DEFAULT_CAP}


async def serve(client: MossClient, info: dict, host: str = "127.0.0.1",
                port: int = 8765, open_browser: bool = True) -> None:
    loop = asyncio.get_running_loop()
    app = App(client, loop, info)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://{host}:{port}/"
    print(f"reader open at {url}   (Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        await asyncio.Event().wait()
    finally:
        httpd.shutdown()
