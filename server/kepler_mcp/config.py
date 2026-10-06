"""Runtime settings for the kepler.gl MCP server.

Every value resolves from the environment first, then from an optional JSON
config file, then from the defaults below.

The defaults are chosen so that the *local* half of the plugin works with no
configuration at all. Creating a map from a CSV and opening it is a filesystem
operation and nothing else, and it must not be gated behind an Auth0 tenant
having been set up first — that is the whole point of "if the user doesn't log
in, they can still create local maps". So the identity-provider settings below
have no defaults and are only required by `login`, not by `validate`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_PREFIX = "KEPLER_GL_"

#: Where the hosted half of the plugin lives. Local development points at the
#: Next.js dev server; a deployment sets this to the Vercel URL.
DEFAULT_SERVER_URL = "http://localhost:3000"

#: The loopback port the OAuth redirect listener binds.
#:
#: Fixed rather than ephemeral, and that is a constraint rather than a
#: preference: Auth0 redirects only to a callback URL registered against the
#: client, and a registered URL carries its port. An ephemeral port would be a
#: callback-mismatch page on every login. Register
#: `http://127.0.0.1:8976/callback` against the Auth0 application — the server
#: repo's README says so too — or override this and register the override.
#:
#: 8976 is high and unusual on purpose: 3000 belongs to the Next.js dev server,
#: 8080 and 8000 to whatever else is running, and a collision here is a failed
#: login with a legible cause only if you already know to look for it.
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8976/callback"

DEFAULT_CONFIG_DIR = Path(
    os.environ.get(f"{ENV_PREFIX}CONFIG_DIR", Path.home() / ".config" / "kepler-gl")
)


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"{ENV_PREFIX}{name}") or default


def _flag(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # --- the hosted half ------------------------------------------------
    server_url: str = DEFAULT_SERVER_URL

    # --- identity provider ----------------------------------------------
    # Auth0, in an application of type "Native". A native application is the
    # right kind for this: it is a public client with no secret, so PKCE is the
    # only proof it can offer, and Auth0 permits a loopback redirect for it —
    # which is exactly the flow `auth.py` runs.
    auth0_domain: str = ""
    auth0_client_id: str = ""
    auth0_audience: str = ""
    # `offline_access` is what buys a refresh token, so a login survives a
    # restart instead of prompting every session.
    auth0_scope: str = "openid profile email offline_access"
    redirect_uri: str = DEFAULT_REDIRECT_URI

    # --- testing overrides ----------------------------------------------
    # A pinned bearer token, used instead of signing in. Gated behind a second
    # switch so a token left in the environment — which is how one gets left
    # behind — cannot silently become the production path.
    dev_token: str | None = None
    dev_mode: bool = False

    # --- paths -----------------------------------------------------------
    config_dir: Path = field(default_factory=lambda: DEFAULT_CONFIG_DIR)
    #: Where map directories are written. Deliberately not under `config_dir`:
    #: these are artifacts the user opens, sends to colleagues and keeps, not
    #: state the tool manages. A hidden directory in `$HOME` is where a file
    #: goes to be forgotten.
    map_dir: Path | None = None

    #: The port the preview server binds. 0 — the default — asks the OS for a
    #: free one, which is the right answer when two plugin processes are
    #: running. It is settable because a user behind a proxy or a corporate
    #: firewall may need a port they know in advance, and because the preview
    #: server's own error text names this variable as the fix.
    preview_port: int = 0

    @property
    def token_path(self) -> Path:
        return self.config_dir / "token.json"

    @property
    def maps_dir(self) -> Path:
        """The directory holding one subdirectory per map."""
        if self.map_dir is not None:
            return self.map_dir
        raw = _env("MAP_DIR")
        return Path(raw) if raw else Path.home() / "kepler-maps"

    @property
    def viewer_available(self) -> bool:
        """Whether a bundle has been recorded next to this package."""
        return self.viewer_version is not None

    @property
    def viewer_version(self) -> str | None:
        """The version stamp of the shipped bundle, or None if there is none.

        Read from `plugins/kepler.gl/vendor/kepler-viewer.version`, which
        `viewer/build.mjs` writes from a hash of the sources it bundled. The
        stamp exists so a stale bundle can be *noticed*: a checkout whose vendor
        directory is older than its viewer sources renders maps with whatever
        the viewer used to do, and nothing about the result says so.
        """
        path = self.vendor_dir / "kepler-viewer.version"
        try:
            return path.read_text().strip() or None
        except OSError:
            return None

    @property
    def vendor_dir(self) -> Path:
        """The plugin's `vendor/` directory, found through the installed package.

        Resolved from `__file__` rather than from an environment variable,
        because the launcher that starts this process has already had to answer
        the same question — "where is this checkout" — and answering it twice in
        two ways is two things to get wrong. `server/kepler_mcp/config.py` is
        three levels below the plugin root in a source checkout, and the wheel
        built by `pyproject.toml` keeps that shape.
        """
        override = _env("VENDOR_DIR")
        if override:
            return Path(override)
        return Path(__file__).resolve().parents[2] / "plugins" / "kepler.gl" / "vendor"

    @property
    def viewer_bundle(self) -> Path:
        return self.vendor_dir / "kepler-viewer.js"

    @classmethod
    def load(cls) -> "Settings":
        settings = cls()
        config_file = settings.config_dir / "config.json"
        if config_file.exists():
            try:
                data = json.loads(config_file.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
            for key, value in data.items():
                if hasattr(settings, key) and value is not None:
                    setattr(settings, key, value)

        # Env always wins over the config file.
        for key in (
            "server_url",
            "auth0_domain",
            "auth0_client_id",
            "auth0_audience",
            "auth0_scope",
            "redirect_uri",
            "dev_token",
            "map_dir",
        ):
            value = _env(key.upper())
            if value:
                setattr(settings, key, Path(value) if key == "map_dir" else value)

        settings.dev_mode = _flag("DEV_MODE", settings.dev_mode)
        raw_port = _env("PREVIEW_PORT")
        if raw_port:
            try:
                settings.preview_port = int(raw_port)
            except ValueError as exc:
                raise ValueError(
                    f"KEPLER_GL_PREVIEW_PORT must be a port number, got {raw_port!r}"
                ) from exc
        return settings

    def validate(self) -> None:
        """Reject configuration that would fail later, less legibly.

        Only the shape of what is set is checked. A missing Auth0 client is not
        an error here — it is a working local-only install, and `login` is where
        it becomes one.
        """
        if not self.server_url.startswith(("http://", "https://")):
            raise ValueError(
                f"server_url must be an http(s) URL, got {self.server_url!r}"
            )
        if self.server_url.endswith("/"):
            raise ValueError(
                "server_url must not end with a slash — the paths appended to it "
                "already begin with one"
            )
        if not self.redirect_uri.startswith(("http://", "https://")):
            raise ValueError(
                f"redirect_uri must be an http(s) URL, got {self.redirect_uri!r}"
            )
        if "?" in self.redirect_uri or "#" in self.redirect_uri:
            raise ValueError("redirect_uri must not carry a query string or fragment")
        # Auth0 compares the redirect URI as a *string* against the registered
        # callback, so the listener has to bind the port this names. Port 80 and
        # 443 are excluded because binding them usually needs privileges, and
        # the failure then arrives as a permission error nowhere near the cause.
        port = self.redirect_port
        if port is None:
            raise ValueError(
                f"redirect_uri must name an explicit port, got {self.redirect_uri!r} — "
                "Auth0 matches the callback as a string, and a URI with no port is "
                "a different string"
            )
        if port < 1024:
            raise ValueError(
                f"redirect_uri port {port} needs privileges to bind; use a port "
                "above 1023"
            )
        if not 0 <= self.preview_port <= 65535:
            raise ValueError(
                f"preview_port must be 0 (let the OS choose) or a port number, "
                f"got {self.preview_port}"
            )
        if self.dev_token and not self.dev_mode:
            raise ValueError(
                "KEPLER_GL_DEV_TOKEN is set but KEPLER_GL_DEV_MODE is not. Set "
                "KEPLER_GL_DEV_MODE=1 to use a pinned token."
            )
        for name, value in (
            ("auth0_domain", self.auth0_domain),
            ("auth0_client_id", self.auth0_client_id),
        ):
            if value and any(ch.isspace() for ch in value):
                raise ValueError(f"{name} must not contain whitespace, got {value!r}")

    @property
    def redirect_port(self) -> int | None:
        from urllib.parse import urlparse

        return urlparse(self.redirect_uri).port

    @property
    def login_configured(self) -> bool:
        """Whether enough is set to attempt a sign-in at all."""
        return bool(self.auth0_domain and self.auth0_client_id and self.auth0_audience)
