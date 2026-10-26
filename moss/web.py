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

from .client import MossClient, MossError, Progress

INDEX = Path(__file__).with_name("index.html")
STATIC = Path(__file__).with_name("static")          # bundled KaTeX, so math works offline
STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".woff2": "font/woff2"}
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class Job:
    def __init__(self, kind: str, query: str):
        self.id = secrets.token_hex(6)
        self.kind, self.query = kind, query
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
                                for r in out.results]}
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

    def submit(self, kind: str, query: str, refresh: bool) -> Job:
        # Forget old jobs so the table doesn't grow forever.
        for jid in [j for j, v in self.jobs.items()
                    if v.state != "running" and time.time() - v.created > 3600]:
            del self.jobs[jid]
        job = Job(kind, query)
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
            try:
                size = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(size, 4096)) or b"{}")
            except (ValueError, OSError):
                return self._json({"error": "bad body"}, 400)
            path = urlparse(self.path).path
            if path == "/api/search":
                q = str(body.get("q", "")).strip()
                if not q:
                    return self._json({"error": "type something to search for"}, 400)
                self._json(app.submit("search", q, bool(body.get("refresh"))).to_dict())
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
