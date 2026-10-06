#!/usr/bin/env python3
"""A static server for the viewer test pages, with caching switched off.

`python3 -m http.server` sends no `Cache-Control`, which leaves the browser to
cache heuristically off `Last-Modified`. That is fine for a page you load once
and terrible for the loop this exists to serve: rebuild the 13 MB bundle, reload,
and the browser hands back the previous build while the version stamp in the
page still says the new one. Every debugging session therefore starts by
suspecting the wrong layer.

`no-store` on everything makes a reload mean a reload. The pages here are a
development fixture, not something shipped, so there is nothing to lose.

Usage:  python3 serve.py [port]      (default 8919)
"""

from __future__ import annotations

import functools
import http.server
import json
import socketserver
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


class NoStoreHandler(http.server.SimpleHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 — the name is http.server's
        """Accept the viewer's save POST, the way a host page would.

        `spec.js` points the Save button at `/__save`, so without this the only
        thing the smoke test can prove is that the button is wired to *a*
        request. Recording the body proves the round trip: the config that came
        out of the store, as JSON, at the moment Save was pressed.

        The written file is `last-save.json`, next to this script. It is a
        fixture output, so it is gitignored rather than committed.
        """
        if self.path.split("?")[0] != "/__save":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            self.send_error(400, f"not JSON: {exc}")
            return

        (HERE / "last-save.json").write_bytes(body)
        config = parsed.get("config") or {}
        layers = (config.get("config") or {}).get("visState", {}).get("layers", [])
        print(f"saved map {parsed.get('mapId')!r}: {len(layers)} layer(s), {len(body)} bytes")

        payload = json.dumps({"ok": True, "layers": len(layers)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        # The default logger writes a line per request, and the bundle is
        # requested once per reload — enough noise to bury a real error.
        if "200" not in (fmt % args):
            super().log_message(fmt, *args)


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8919
    handler = functools.partial(NoStoreHandler, directory=str(HERE))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        print(f"viewer test pages on http://localhost:{port}/  (serving {HERE})")
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
