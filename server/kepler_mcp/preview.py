"""The loopback server that makes a local map editable.

A map directory opened from Finder renders but cannot save: a `file://` page has
no origin to POST to, and it cannot fetch a sibling Parquet file either. Opening
the same directory over `http://127.0.0.1:<port>/` fixes both, and this is the
server that does it.

Three things it has to get right, each of which is a bug if it does not.

**Range requests.** The viewer reads Parquet with hyparquet, which asks for the
footer first and the row groups after — a handful of ranged reads rather than a
full download. Python's `SimpleHTTPRequestHandler` has no notion of `Range`: it
answers a ranged request with the whole file and a 200, and hyparquet, which
asked for bytes 4096-8191, reads the first 4 KB of a Parquet file as though it
were part of the footer. The result is not an error anyone can act on. So the
range handling below is not an optimisation, it is what makes a Parquet-backed
local map work at all.

**A save endpoint reachable from any page in the browser.** This server is on
localhost, so every page the user visits can reach it — and one that POSTs here
would rewrite a map's saved config. It is bound to the loopback interface,
which keeps it off the network, but that is not a defence against a page
*already in the browser* talking to it. The defence is a token minted at
start-up, embedded in the spec the page carries, and required back on the POST.
A cross-origin page cannot read the spec, so it cannot know the token.

**Threading.** The page issues its dataset fetches concurrently, and a
single-threaded server serialises them behind a Parquet read. `ThreadingHTTPServer`
is the whole fix.

**And the root is the index.** This server also answers `/` — not with a
directory listing, which is what a stock handler would do, but with the map
index: every map on the machine as one page. It is here rather than in a file
beside the maps for the reason `gallery.py` gives — a written page drifts from
the directories it describes, and a rendered one cannot.
"""

from __future__ import annotations

import json
import mimetypes
import re
import secrets
import threading
from datetime import datetime, timezone
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .gallery import render_index
from .store import HTML_NAME, LocalStore, StoreError

#: The header the save request must carry, and the value it must hold. Sent as a
#: custom header rather than as a query parameter, because the viewer's
#: `SaveTarget` has a `headers` field and no notion of a query string, and
#: because a token in a URL ends up in history and in logs.
SAVE_TOKEN_HEADER = "X-Kepler-Save-Token"

#: The path a map's save button posts to, within the map's own directory.
SAVE_PATH = "__save"

#: Matches `Range: bytes=<start>-<end>`. Only the single-range form is handled:
#: it is the only form hyparquet sends, and a multipart/byteranges response is a
#: great deal of code for a case that does not arise from this viewer.
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class PreviewServer:
    """Serves a maps directory over loopback, with a save endpoint."""

    def __init__(self, store: LocalStore, *, port: int = 0, host: str = "127.0.0.1") -> None:
        self.store = store
        self.host = host
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> str:
        """Bind and serve in a background thread. Returns the base URL.

        Serves the maps root rather than one map's directory, so a single
        server covers every map the user has and the bundle at
        `../kepler-viewer.js` resolves for all of them.
        """
        if self._httpd is not None:
            return self.base_url

        self.store.ensure()
        handler = partial(_Handler, directory=str(self.store.root))
        try:
            # `port=0` asks the OS for a free one. Unlike the OAuth redirect,
            # there is no registered callback pinning this port — the page's
            # save target is a relative URL — so an ephemeral port is both
            # available and better: two plugin processes do not collide.
            self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as exc:
            raise StoreError(
                f"Cannot listen on {self.host}:{self.port} to preview maps — {exc}. "
                f"Set KEPLER_GL_PREVIEW_PORT to 0 to let the OS choose a free port."
            ) from exc

        self.port = self._httpd.server_address[1]
        # The token is read by the handler on each request, and the handler is
        # constructed per request from the class, so it is attached to the class
        # rather than to an instance.
        _Handler.save_token = self.token
        _Handler.store = self.store

        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2}
        )
        self._thread.daemon = True
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        self._httpd = None
        self._thread = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    # -- URLs --------------------------------------------------------------

    def url_for(self, slug: str, filename: str = HTML_NAME) -> str:
        """The URL of a file inside a map's directory.

        `map.html` is written as the directory's `index.html` equivalent by
        naming it explicitly in the path, because a browser given a directory
        URL would otherwise list it.
        """
        return f"{self.base_url}/{slug}/{filename}"

    def map_url(self, slug: str) -> str:
        return self.url_for(slug)

    @property
    def index_url(self) -> str:
        """The URL of the map index — every local map, as one page.

        Served at the root rather than at a filename, so it is the thing a user
        gets by opening the preview server's address and nothing else. The page
        is rendered per request from a fresh scan; see `gallery.py`.
        """
        return f"{self.base_url}/"

    def save_target(self, slug: str) -> dict:
        """The `SaveTarget` the map's page carries.

        Relative to the page, so the same spec works from any port and from the
        hosted app, which renders its own save path. The token is what makes the
        relative URL safe to leave in a page that any browser can reach.
        """
        return {
            "url": f"./{SAVE_PATH}",
            "headers": {SAVE_TOKEN_HEADER: self.token},
            "label": "Save map",
        }


class _Handler(SimpleHTTPRequestHandler):
    """Static files with range support, plus the one POST endpoint."""

    #: Injected on the class by `PreviewServer.start`. Class attributes rather
    #: than constructor arguments because `SimpleHTTPRequestHandler` is
    #: instantiated by the server per request, with the server itself as the
    #: only argument.
    save_token: str = ""
    store: LocalStore

    #: Set on a request that is about to return a file. Read by `end_headers`
    #: to add `Accept-Ranges` to the response, which is what tells a reader it
    #: is allowed to ask for a slice — hyparquet checks it before issuing the
    #: ranged reads that a Parquet footer needs.
    _advertise_ranges = False

    server_version = "kepler-gl-mcp"

    def end_headers(self) -> None:
        if self._advertise_ranges:
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    # -- GET / HEAD --------------------------------------------------------

    def send_head(self):
        """Answer a ranged request with 206 and the slice, or fall through.

        Delegating everything else to `SimpleHTTPRequestHandler.send_head` keeps
        the directory-index refusal, the `..` traversal guard and the last-modified
        handling that already work, and confines the added behaviour to the case
        that needs it.
        """
        # The root is a page, not a directory. Handled before anything else
        # because the maps root *is* a directory, and a stock handler would
        # answer this path with a listing or a refusal — neither of which is the
        # index. `index.html` is accepted too, so a browser that appends it
        # lands on the same page.
        if urlparse(self.path).path in ("/", "/index.html"):
            return self._send_index()

        # Checked before delegating, so the header is on the response that is
        # about to be written. `translate_path` runs again below or in the
        # superclass; a second `stat` on a path this handler is already going to
        # read is not worth avoiding.
        #
        # Assigned rather than set, because one handler instance serves every
        # request on a kept-alive connection — a flag left over from a file
        # response would put `Accept-Ranges` on the next POST's JSON.
        self._advertise_ranges = Path(self.translate_path(self.path)).is_file()

        header = self.headers.get("Range")
        if not header:
            return super().send_head()

        match = _RANGE_RE.fullmatch(header.strip())
        if not match:
            # A range form this server does not implement. Ignoring the header
            # and sending the whole file with a 200 is what the specification
            # permits for an unsatisfiable or unsupported range.
            return super().send_head()

        path = self.translate_path(self.path)
        file = Path(path)
        if not file.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None

        size = file.stat().st_size
        start_text, end_text = match.groups()
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
        elif end_text:
            # A suffix range: the last N bytes. hyparquet uses this form for the
            # footer when it does not know the file's length yet.
            start = max(0, size - int(end_text))
            end = size - 1
        else:
            start, end = 0, size - 1

        if start >= size or start > end:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        end = min(end, size - 1)

        handle = file.open("rb")
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Type", self.guess_type(path))
        # `Accept-Ranges` comes from `end_headers`, which knows this is a file.
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Last-Modified", self.date_time_string(int(file.stat().st_mtime)))
        # No caching. A map is re-rendered in place, and a cached `map.html`
        # after a save shows the user their previous config and no way to tell.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        handle.seek(start)
        remaining = end - start + 1
        if self.command == "HEAD":
            handle.close()
            return None
        return _Slice(handle, remaining)

    def copyfile(self, source, outputfile) -> None:
        """Copy, honouring the byte limit a ranged response set up."""
        if isinstance(source, _Slice):
            remaining = source.remaining
            while remaining > 0:
                chunk = source.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                outputfile.write(chunk)
            source.close()
            return
        super().copyfile(source, outputfile)

    def guess_type(self, path) -> str:
        """Content types for the extensions browsers do not know by default.

        `.parquet` is unregistered, so `mimetypes` returns
        `application/octet-stream` — which fetch will hand back as a blob
        happily, but a `Content-Type` that says nothing is a poor thing to leave
        in a response whose whole purpose is to be decoded.
        """
        lowered = str(path).lower()
        if lowered.endswith(".parquet"):
            return "application/vnd.apache.parquet"
        if lowered.endswith((".geojson", ".json")):
            return "application/json"
        return super().guess_type(path)

    # -- the index ---------------------------------------------------------

    def _send_index(self):
        """Render the map index from a fresh scan and send it.

        Rendered per request rather than stored, so a map deleted from Finder is
        gone from the page on the next reload — the same reason `list_maps`
        scans instead of reading an index file. That costs one `spec.json` read
        per map per request, which is what `list_maps` already costs.
        """
        self._advertise_ranges = False
        try:
            maps = self.store.list_maps()
        except OSError:
            # A page that says "nothing yet" beats a 500 out of a broken root.
            maps = []
        body = render_index(
            maps,
            root=self.store.root,
            bundle_present=not self.store.bundle_missing(),
        ).encode("utf-8")

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Same reasoning as a map page: the listing changes as maps are made and
        # deleted, and a cached one shows a map the user just removed.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return None

    # -- POST --------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        # This handler instance may have served a file on the same connection.
        self._advertise_ranges = False
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) != 2 or parts[1] != SAVE_PATH:
            self._json_error(HTTPStatus.NOT_FOUND, "Nothing is served here.")
            return

        slug = unquote(parts[0])
        # The token is the only thing standing between a map's saved config and
        # any page the user happens to have open, so it is checked before the
        # body is read: an unauthorised request should not get to make this
        # process parse arbitrary JSON.
        if not self.save_token or self.headers.get(SAVE_TOKEN_HEADER) != self.save_token:
            self._json_error(
                HTTPStatus.FORBIDDEN,
                "This save endpoint is not open to this page. Reload the map from "
                "the URL the plugin gave you.",
            )
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._json_error(HTTPStatus.BAD_REQUEST, "Empty request body.")
            return

        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError) as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, f"Body is not JSON — {exc}")
            return

        config = payload.get("config") if isinstance(payload, dict) else None
        if not isinstance(config, dict) or "config" not in config:
            self._json_error(
                HTTPStatus.BAD_REQUEST,
                "Expected `{mapId, config}` where config is a saved kepler config.",
            )
            return

        try:
            record = self.store.get(slug)
        except StoreError as exc:
            self._json_error(HTTPStatus.NOT_FOUND, str(exc))
            return

        # Only the config is replaced. The datasets, the title and the save
        # target are the plugin's to own; the viewer knows about the config and
        # nothing else, so anything else it sent would be a guess.
        spec = dict(record.spec)
        spec["config"] = config
        # From here on there is a viewport somebody chose. Until now `centreMap`
        # was on, so the map fitted its data on open; leaving it on would refit
        # on every reopen and silently discard the zoom and pan the user just
        # saved — which is half of what they saved.
        spec["centreMap"] = False
        try:
            self.store.write_map(slug, spec)
        except StoreError as exc:
            self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return

        saved_at = datetime.now(timezone.utc)
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "slug": slug,
                "savedAt": saved_at.isoformat(),
                "configPath": str(record.spec_path),
            },
        )

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        """Refuse preflight, deliberately.

        A cross-origin page that tries to POST here is stopped by the token
        check. Answering the preflight would let it through to that check
        anyway, so the honest answer is that this endpoint takes no cross-origin
        requests at all.
        """
        self._advertise_ranges = False
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, HEAD, POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, status: HTTPStatus, message: str) -> None:
        self._json(status, {"ok": False, "error": message})

    def log_message(self, fmt: str, *args) -> None:
        """Keep the access log, but off stderr.

        The stdio transport owns stdout and uses stderr for diagnostics; an
        access line per request would bury a real message under a page's worth
        of asset fetches. `logging` at DEBUG keeps it available to anyone who
        asks and out of the way of anyone who does not.
        """
        import logging

        logging.getLogger(__name__).debug("%s - %s", self.address_string(), fmt % args)


class _Slice:
    """A read-limited view of an open file, for `copyfile`."""

    def __init__(self, handle, remaining: int) -> None:
        self._handle = handle
        self.remaining = remaining

    def read(self, size: int) -> bytes:
        return self._handle.read(size)

    def close(self) -> None:
        self._handle.close()


# ---------------------------------------------------------------------------
# One server per maps directory
# ---------------------------------------------------------------------------

_servers: dict[str, PreviewServer] = {}
_servers_lock = threading.Lock()


def server_for(store: LocalStore, *, port: int = 0) -> PreviewServer:
    """The preview server for a maps directory, started on first use.

    Kept alive for the life of the process rather than started and stopped per
    call: an MCP server serves a whole conversation, the user may open several
    maps from it, and a map already open in a tab must keep working when the
    next tool call runs. Restarting it per call would break the tab that is
    still open — silently, since the page would simply stop being able to save.
    """
    key = str(store.root.resolve())
    with _servers_lock:
        existing = _servers.get(key)
        if existing is not None and existing.running:
            return existing
        server = PreviewServer(store, port=port)
        server.start()
        _servers[key] = server
        return server


def stop_all() -> None:
    """Shut down every preview server. For tests and for process exit."""
    with _servers_lock:
        for server in _servers.values():
            server.stop()
        _servers.clear()


# `mimetypes` is initialised from the system's databases on first use, and on a
# bare container that can leave `.js` unregistered — which matters, because a
# bundle served as `text/plain` is refused by the browser's module and classic
# script checks depending on how it was loaded.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/json", ".geojson")
