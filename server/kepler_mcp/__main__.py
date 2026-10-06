"""stdio entry point: `kepler-gl-mcp`.

Registered by `pyproject.toml` as a console script, and by `.mcp.json` for the
Claude Code and Codex plugins, which start it as a subprocess and speak JSON-RPC
over stdin/stdout.

Nothing here may write to stdout. stdout is the transport: a stray print
corrupts the frame stream and the host reports a protocol error rather than
whatever was printed. Logging goes to stderr, which the host captures — and so
does the sign-in URL, which is why `auth.py` prints it there rather than to
stdout.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import __version__
from .auth import load_dev_token_env
from .config import Settings
from .preview import stop_all
from .tools import KeplerApp

#: Load-bearing workflow context. An MCP client shows this to its model before
#: any tool call, which is the only channel available to a host that has no
#: plugin skills — Codex, for one. The Claude Code plugin ships the same
#: guidance as a skill; this is the portable floor, not a duplicate of it.
INSTRUCTIONS = """\
Create kepler.gl maps from data files, and optionally keep them on a server.

The local half needs no account and no network. The hosted half needs `login`.

1. `auth_status` first, once per session. It reports whether a kepler.gl account
   is connected. Creating and editing local maps does not need one — do not ask
   a user to sign in to make a map, and never call `login` just to be ready.

2. `inspect_data` before `create_map` whenever the data is unfamiliar. It
   returns the columns and their types, which columns the plugin takes as the
   geometry, and which layer type that implies — and it writes nothing. A file
   with no geometry is worth knowing about before the map is built, not after.

3. `create_map` builds the map and returns a URL. **Give the user the URL, not
   the file path.** The page is published on a loopback port because a `file://`
   page can neither save its edits nor fetch its own Parquet data — the path
   renders but cannot be edited, so handing it over produces a map that looks
   finished and is not. The path belongs in the answer too, as the thing the
   user keeps or shares.

   The map is a directory: `map.html`, `spec.json`, and `data/*.parquet` when
   the rows are too large to inline. The Save button in the page writes back to
   `spec.json` through the same server. So "open the URL, change the map, save
   it" is the editing loop — do not rebuild a map to change its layers; open it.

4. `list_maps` and `open_map` are how a map made earlier is found again.
   `open_map` re-renders the page, so a map created before a plugin update
   picks up the current viewer.

5. To keep a map on the server: `list_projects`, then `upload_map` with a
   project name or id — the project is created if it does not exist.
   `update_server_map` pushes a locally-edited map over a hosted one, keeping
   its id and URL. `open_server_map` returns the URL for a hosted map, where
   the same viewer and the same Save button write to the server.

Two things not to do. Do not pass the contents of a data file in a message —
the tools take paths and URLs, and `inspect_data` is what reports the schema.
Do not delete anything on the user's behalf without their agreement to that
specific map: `delete_map` and `delete_server_map` ask for confirmation, and if
the confirmation card is unavailable they refuse rather than proceed.
"""


def _load_settings() -> Settings:
    """Settings plus the optional local pinned-token file.

    The file is a development convenience: a `dev-token.env` holding a token
    minted from a browser session, so a test run does not open one. It has to
    clear the same two-key gate as the environment (`KEPLER_GL_DEV_TOKEN` *and*
    `KEPLER_GL_DEV_MODE`), so a stray file cannot become the production path on
    its own — but both keys may come from the file, so dropping it in place is
    enough.
    """
    settings = Settings.load()
    if settings.dev_token:
        return settings

    for candidate in (
        settings.config_dir / "dev-token.env",
        # The plugin's own checkout, which is where a developer running the
        # server from source will have put it.
        Path(__file__).resolve().parents[2] / "dev-token.env",
        Path.cwd() / "dev-token.env",
    ):
        values = load_dev_token_env(candidate)
        token = values.get("KEPLER_GL_DEV_TOKEN")
        if not token:
            continue
        settings.dev_token = token
        settings.dev_mode = settings.dev_mode or values.get(
            "KEPLER_GL_DEV_MODE", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        break
    return settings


def build_server(settings: Settings, app: KeplerApp | None = None) -> MCPServer:
    """Wire the tools onto a server.

    `app` is an injection seam. The app owns the map store, which is where every
    local map is written, so a caller that wants to point the tools at a
    temporary directory has to hand in the same instance they will use —
    otherwise the tools build a second one and the setup is silently discarded.
    """
    server = MCPServer(
        name="kepler-gl",
        title="kepler.gl",
        version=__version__,
        instructions=INSTRUCTIONS,
    )
    (app or KeplerApp(settings)).register(server)
    return server


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = _load_settings()
        settings.validate()
    except ValueError as exc:
        # Fail with the reason on stderr. The host surfaces it as a failed
        # server start, which is far easier to diagnose than a connection that
        # opens and then errors on every call.
        print(f"kepler-gl: invalid configuration — {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    try:
        build_server(settings).run("stdio")
    finally:
        # The preview server is a daemon thread bound to a port. Leaving it to
        # the interpreter is usually fine and occasionally not: a shutdown that
        # races the accept loop prints a traceback out of `socketserver` on the
        # way out, which reads as a crash rather than as a close.
        stop_all()


if __name__ == "__main__":
    main()
