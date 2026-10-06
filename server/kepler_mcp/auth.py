"""Signing in to the kepler.gl server, from a process that is not a browser.

The plugin's hosted half — listing maps, uploading a config, storing a dataset —
lives behind the Next.js application in `kepler-plugin-server`, which
authenticates with Auth0. This module is the other end of that: an
authorization-code flow with PKCE, run by a local process, with the redirect
caught on a loopback port.

Three things about this flow are worth knowing before debugging it, because each
has produced a bug that named something other than its cause.

**The port is not free to choose.** Auth0 redirects only to callback URLs
registered against the application, and a registered URL carries its port and
its path. So the listener binds the host and port named by
`Settings.redirect_uri` — `http://127.0.0.1:8976/callback` by default — rather
than taking an ephemeral port the way a local server normally would. A
mismatch comes back as an HTML callback-mismatch page in the browser and a
listener that sits there until it times out, which reads as "the login is
broken" rather than "the port is wrong".

**PKCE is the whole of the client's proof.** The Auth0 application is of type
Native, which is a public client: there is no secret to send, and shipping one
in a plugin would not be a secret anyway. The code verifier is generated per
login, held only in this process, and never written to disk — what lands in the
token cache is the result of the exchange, not the means to repeat it.

**The token cache is a live credential.** It holds an access token for the API
and a refresh token that mints more, so it is written `0600` and replaced
atomically; `.gitignore` excludes `token.json` for the case where someone sets
`config_dir` to a checkout by mistake.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

from .config import Settings

log = logging.getLogger(__name__)

#: Auth0 and the hosted API are called synchronously from tool handlers, so
#: allow generous headroom without hanging forever.
HTTP_TIMEOUT = 30.0

#: How long the browser sign-in may take before the listener gives up. Five
#: minutes is long enough to find a password manager and short enough that a
#: forgotten tab does not hold a tool call open indefinitely.
LOGIN_TIMEOUT = 300.0


class AuthError(RuntimeError):
    """Any failure in the login chain, with a message meant for a human."""


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def jwt_claims(token: str) -> dict:
    """The payload of a JWT, decoded and *not* verified.

    For display only — the email address to print in a status report. Nothing
    is authorised on the strength of this: the server verifies the signature
    against Auth0's JWKS on every request, and that is the check that matters.
    Decoding here is a convenience so `auth_status` can say *who* is signed in
    rather than only that someone is.
    """
    try:
        payload = token.split(".")[1]
        # The payload is base64url with the padding stripped.
        padded = payload + "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}


@dataclass
class CallbackResult:
    code: str | None = None
    state: str | None = None
    error: str | None = None
    error_description: str | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    """Single-shot handler that captures the authorization code.

    It answers on any path. Auth0 sends the response to the path registered as
    the callback — `/callback` by default — but refusing anything else buys
    nothing and costs a confusing failure the first time someone registers the
    redirect at the origin and points `KEPLER_GL_REDIRECT_URI` at it.
    """

    result: CallbackResult
    done: threading.Event

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = params.get("code", [None])[0]
        error = params.get("error", [None])[0]
        if not code and not error:
            # A favicon fetch, a probe, a reload. Refused rather than treated as
            # the callback, so it cannot overwrite a result that already
            # arrived.
            self.send_response(404)
            self.end_headers()
            return

        # Assign the fields, never rebind `self.result`. The object is injected
        # on the handler *class*, so it is the instance the caller reads back;
        # `self.result = ...` would shadow it with a new object and the caller
        # would keep seeing the empty original.
        self.result.code = code
        self.result.state = params.get("state", [None])[0]
        self.result.error = error
        self.result.error_description = params.get("error_description", [None])[0]

        body = (
            b"<!doctype html><meta charset='utf-8'><title>Signed in</title>"
            b"<body style='font-family:system-ui;padding:3rem;max-width:32rem'>"
            b"<h2>Signed in to kepler.gl.</h2>"
            b"<p>You can close this tab and go back to your agent.</p></body>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.done.set()

    def log_message(self, *args) -> None:
        """Silence the default stderr access log.

        stderr is where this server's diagnostics go and the host captures it,
        so a line per request would be noise around the one line that matters.
        """


def _serve_callback(
    host: str,
    port: int,
    url_for_port: Callable[[int], str],
    *,
    timeout: float = LOGIN_TIMEOUT,
) -> CallbackResult:
    """Bind the redirect listener, open the browser, and return what arrived."""
    result = CallbackResult()
    done = threading.Event()
    handler = type("_Handler", (_CallbackHandler,), {"result": result, "done": done})

    try:
        # No SO_REUSEADDR: something already on this port has to fail loudly
        # rather than let two listeners race for the callback.
        server = HTTPServer((host, port), handler)
    except OSError as exc:
        raise AuthError(
            f"Cannot listen on {host}:{port} for the sign-in redirect — "
            f"{exc.strerror or exc}. Auth0 will only redirect to a registered "
            f"callback URL, and this one is `{url_for_port(port)}`. Stop whatever "
            f"holds the port, or set KEPLER_GL_REDIRECT_URI to a free one and "
            f"register that with Auth0 as well."
        ) from exc
    server.timeout = 1.0

    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2})
    thread.daemon = True
    thread.start()

    try:
        open_url = url_for_port(port)
        # Always name the URL, not only when opening a browser fails. Under the
        # stdio transport the browser may open behind the agent's window, or in
        # a profile that is signed in as somebody else; without the URL on
        # screen the only symptom is a login that sits there until it times out.
        # stderr, not stdout — stdout is the JSON-RPC frame stream.
        print(
            f"\nComplete the sign-in in a browser at:\n\n{open_url}\n\n"
            f"Waiting for the redirect to http://{host}:{port}/ …\n",
            file=sys.stderr,
            flush=True,
        )
        webbrowser.open(open_url)

        if not done.wait(timeout=timeout):
            raise AuthError(
                f"Timed out after {int(timeout)}s waiting for the browser sign-in. "
                f"Nothing arrived at http://{host}:{port}/. Check that the tab the "
                "sign-in happened in ended up on that address — a sign-in finished "
                "in some other application's tab never reaches this listener."
            )
        captured = handler.result
    finally:
        server.shutdown()
        server.server_close()

    return captured


class TokenProvider:
    """Resolves a usable bearer token for the hosted API, caching and refreshing."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._memo: str | None = None

    # -- cache -------------------------------------------------------------

    def _read_cache(self) -> dict:
        path = self.settings.token_path
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_cache(self, data: dict) -> None:
        path = self.settings.token_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        # The file holds a live access token and a refresh token. Owner-only,
        # and set on the temporary file before the rename so the credential is
        # never briefly world-readable under its final name.
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    def clear(self) -> None:
        self._memo = None
        try:
            self.settings.token_path.unlink()
        except FileNotFoundError:
            pass

    # -- the flow ----------------------------------------------------------

    def _authorize_url(self, state: str, challenge: str) -> str:
        s = self.settings
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": s.auth0_client_id,
                "redirect_uri": s.redirect_uri,
                "scope": s.auth0_scope,
                "audience": s.auth0_audience,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"https://{s.auth0_domain}/authorize?{query}"

    @staticmethod
    def _raise_for_status(response: httpx.Response, what: str) -> None:
        if response.is_success:
            return
        detail = response.text[:300].strip()
        raise AuthError(
            f"{what} failed: HTTP {response.status_code}"
            f"{f' — {detail}' if detail else ''}"
        )

    def _login(self) -> dict:
        """Run the interactive login and return the credential dict to cache."""
        s = self.settings
        parsed = urllib.parse.urlparse(s.redirect_uri)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(16)

        captured = _serve_callback(
            host, port, lambda _port: self._authorize_url(state, challenge)
        )
        if captured.error:
            raise AuthError(f"Sign-in failed: {captured.error_description or captured.error}")
        if not captured.code:
            raise AuthError("The sign-in callback arrived without an authorization code.")
        # CSRF guard: the state this process generated must come back unchanged.
        if captured.state != state:
            raise AuthError("Sign-in state mismatch — the callback did not match this request.")

        print("Redirect received; exchanging it for a token…", file=sys.stderr, flush=True)
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            response = client.post(
                f"https://{s.auth0_domain}/oauth/token",
                json={
                    "grant_type": "authorization_code",
                    "client_id": s.auth0_client_id,
                    "code": captured.code,
                    "code_verifier": verifier,
                    # Verbatim, and the same string the authorize request sent:
                    # Auth0 compares it against the registered callback.
                    "redirect_uri": s.redirect_uri,
                },
            )
            self._raise_for_status(response, "Auth0 token exchange")
            payload = response.json()

        credentials = self._credentials_from(payload)
        self._write_cache(credentials)
        return credentials

    def _refresh(self, refresh_token: str) -> dict:
        """Swap a refresh token for a new access token. Raises if it is dead."""
        s = self.settings
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            response = client.post(
                f"https://{s.auth0_domain}/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "client_id": s.auth0_client_id,
                    "refresh_token": refresh_token,
                },
            )
            self._raise_for_status(response, "Auth0 refresh")
            payload = response.json()

        credentials = self._credentials_from(payload)
        # Auth0 rotates the refresh token only when the application is
        # configured to; when the response carries no new one, keeping the old
        # one is what makes the *next* refresh work.
        credentials["refresh_token"] = payload.get("refresh_token") or refresh_token
        self._write_cache(credentials)
        return credentials

    @staticmethod
    def _credentials_from(payload: dict) -> dict:
        access_token = payload.get("access_token")
        if not access_token:
            raise AuthError("Auth0 returned no access_token.")
        expires_in = payload.get("expires_in")
        credentials = {
            "access_token": access_token,
            # Epoch seconds, so expiry is comparable without parsing anything.
            # Absent `expires_in` means unknown, which `_expired` treats as "not
            # expired" and leaves the API to reject if it is wrong.
            "expires_at": int(time.time()) + int(expires_in) if expires_in else None,
            "refresh_token": payload.get("refresh_token"),
            "obtained_at": int(time.time()),
            "subject": jwt_claims(payload.get("id_token") or "").get("email"),
        }
        return credentials

    # -- public API --------------------------------------------------------

    def peek_token(self) -> str | None:
        """A usable token that requires no interaction, or None.

        Every path here is silent: the in-process memo, a pinned dev token, the
        on-disk cache, or a refresh exchange. Nothing opens a browser.

        This exists so that asking *about* the auth state cannot itself start a
        sign-in. `auth_status` is the tool an agent calls when something has
        already failed, and having it block for five minutes on a browser window
        — or worse, open one behind the user's back — is the opposite of useful.
        """
        s = self.settings
        if self._memo:
            return self._memo
        if s.dev_token and s.dev_mode:
            self._memo = s.dev_token
            return self._memo

        cache = self._read_cache()
        if cache.get("access_token") and not self._expired(cache.get("expires_at")):
            self._memo = cache["access_token"]
            return self._memo

        # Prefer a silent refresh over making the user sign in again.
        if cache.get("refresh_token") and s.login_configured:
            try:
                credentials = self._refresh(cache["refresh_token"])
                self._memo = credentials["access_token"]
                return self._memo
            except AuthError:
                # Refresh token revoked or expired. The caller falls back to an
                # interactive login; here it just means "nothing usable".
                log.debug("refresh token rejected; a sign-in will be needed")
        return None

    def get_token(self, force_refresh: bool = False) -> str:
        """Return a usable bearer token, signing in if required."""
        s = self.settings
        if not s.login_configured:
            raise AuthError(
                "Signing in needs an Auth0 application. Set KEPLER_GL_AUTH0_DOMAIN, "
                "KEPLER_GL_AUTH0_CLIENT_ID and KEPLER_GL_AUTH0_AUDIENCE — the server "
                "repo's README lists the values its own .env uses, and the Auth0 "
                "application has to have `" + s.redirect_uri + "` registered as a "
                "callback URL. Creating maps locally needs none of this."
            )
        if s.dev_token and not s.dev_mode:
            raise AuthError(
                "KEPLER_GL_DEV_TOKEN is set but KEPLER_GL_DEV_MODE is not. Set "
                "KEPLER_GL_DEV_MODE=1 to use a pinned token."
            )

        if self._memo and not force_refresh:
            return self._memo
        if not force_refresh:
            silent = self.peek_token()
            if silent:
                return silent

        credentials = self._login()
        self._memo = credentials["access_token"]
        return self._memo

    @staticmethod
    def _expired(expires_at) -> bool:
        """Treat an unparseable or absent expiry as "not expired".

        The server is the authority on whether a token is still good; this only
        avoids sending one that is provably stale. Guessing conservatively in
        the other direction would mean a refresh round trip before every call.
        """
        if expires_at is None:
            return False
        if isinstance(expires_at, (int, float)):
            return expires_at <= time.time() + 60
        return False

    def _mode(self) -> str:
        """What will actually be used, not merely what was configured."""
        s = self.settings
        if s.dev_token and s.dev_mode:
            return "pinned-dev-token"
        if s.dev_token:
            # Configured but missing the dev_mode half of the gate.
            return "misconfigured"
        return "anonymous" if not s.login_configured else "oauth"

    def status(self) -> dict:
        cache = self._read_cache()
        return {
            "mode": self._mode(),
            "server_url": self.settings.server_url,
            "has_cached_token": bool(cache.get("access_token")),
            "has_refresh_token": bool(cache.get("refresh_token")),
            "cached_expires_at": cache.get("expires_at"),
            "signed_in_as": cache.get("subject"),
            "token_path": str(self.settings.token_path),
        }


def load_dev_token_env(path: Path) -> dict[str, str]:
    """Read a dotenv-style file of KEPLER_GL_* overrides, if present.

    Convenience for local development: a `dev-token.env` holding a token minted
    from a browser session, so a test run does not open one. Returns every
    recognised key rather than just the token, because the pinned-token path is
    gated on *two* keys and the file has to be able to satisfy both — a file
    that could only supply the token would leave the user to find the second
    switch in the environment, which is not what "drop this file here" should
    mean.

    The gate still holds: both keys must be present *in the file*. A file
    holding only a token changes nothing.
    """
    recognised = {"KEPLER_GL_DEV_TOKEN", "KEPLER_GL_DEV_MODE"}
    if not path.exists():
        return {}
    found: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in recognised:
            cleaned = value.strip().strip("'\"")
            if cleaned:
                found[key] = cleaned
    return found
