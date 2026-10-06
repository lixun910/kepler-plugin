"""The client for the hosted half of the plugin.

`kepler-plugin-server` is a Next.js application that owns projects, maps and
datasets. This module is the only place in the plugin that talks to it.

Two decisions shape everything here.

**The access token is the credential, and the server verifies it.** The server
does not hold a session for this client and there is no cookie exchange: every
call carries `Authorization: Bearer <auth0 access token>` and the server checks
the signature against Auth0's JWKS. That is what lets the plugin be a plain
stdio process rather than something with a login state of its own, and it is why
`auth.py` can be a token *provider* with no notion of a session.

**A 401 means refresh once, then give up.** An access token that was valid when
the process started can expire mid-conversation, and the failure arrives on the
first call after it does. Retrying that call once with a freshly minted token
turns a dead end into a pause; retrying more than once turns a genuine
authorization failure — a token for the wrong audience, a user who was removed —
into an infinite loop that reports nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .auth import AuthError, TokenProvider
from .config import Settings

#: The PUT of a dataset's bytes is the one call that can legitimately take a
#: while — a few hundred megabytes to S3. It does not go through the app, but a
#: slow link is a slow link whichever host is on the other end. Everything else
#: is metadata and should answer immediately.
UPLOAD_TIMEOUT = 600.0
DEFAULT_TIMEOUT = 60.0

#: The content type a presigned PUT is signed for. S3 stores what it was told
#: and the app never reads it back, but a PUT that disagrees with the signature
#: is refused outright, so both sides spell it out rather than guess.
PARQUET_CONTENT_TYPE = "application/octet-stream"


class ApiError(RuntimeError):
    """A call to the hosted app failed, with a message meant for a human.

    `code` is the server's own machine-readable name for the failure, when it
    sent one. It exists so a caller can branch on a specific refusal — the
    upload commit is the one that does — without matching on the prose.
    """

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class RemoteMap:
    """One row of `GET /api/projects/{id}/maps`, as the plugin uses it."""

    id: str
    title: str
    slug: str | None = None
    project_id: str | None = None
    updated_at: str | None = None
    dataset_count: int = 0
    url: str | None = None

    @classmethod
    def from_json(cls, data: dict) -> "RemoteMap":
        return cls(
            id=str(data.get("id", "")),
            # Every field is read defensively. The row arrives from a server
            # that may be a version ahead of this plugin, and a missing `slug`
            # is not a reason to fail a whole listing.
            title=data.get("title") or "(untitled)",
            slug=data.get("slug"),
            project_id=_as_str(data.get("projectId") or data.get("project_id")),
            updated_at=data.get("updatedAt") or data.get("updated_at"),
            dataset_count=int(data.get("datasetCount") or data.get("dataset_count") or 0),
            url=data.get("url"),
        )


@dataclass
class RemoteProject:
    id: str
    name: str
    slug: str | None = None
    map_count: int = 0
    created_at: str | None = None

    @classmethod
    def from_json(cls, data: dict) -> "RemoteProject":
        return cls(
            id=str(data.get("id", "")),
            name=data.get("name") or "(unnamed)",
            slug=data.get("slug"),
            map_count=int(data.get("mapCount") or data.get("map_count") or 0),
            created_at=data.get("createdAt") or data.get("created_at"),
        )


def _as_str(value: Any) -> str | None:
    """Stringify an id without turning None into the string "None".

    Postgres `bigint` ids come back from `JSON.stringify` as numbers, and the
    server may hand them either way; ids compared as strings is the only form
    that works for both.
    """
    if value is None:
        return None
    return str(value)


class ApiClient:
    """Calls against the hosted app, authenticated with an Auth0 access token."""

    def __init__(self, settings: Settings, tokens: TokenProvider) -> None:
        self.settings = settings
        # The provider is held rather than a token, because an MCP server
        # outlives any single access token: a conversation can run for hours
        # against a token that expires in one.
        self.tokens = tokens

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.tokens.get_token()}",
            # Identifies the caller in the app's logs, so a request that shows
            # up there can be traced to this plugin rather than to the web UI.
            "User-Agent": "kepler-gl-mcp",
        }
        if extra:
            headers.update(extra)
        return headers

    def _url(self, path: str) -> str:
        return f"{self.settings.server_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
        retry_on_401: bool = True,
    ) -> Any:
        """One call, with a single silent retry when the token has expired."""
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.request(
                    method,
                    self._url(path),
                    headers=self._headers({"Content-Type": "application/json"}),
                    json=json_body,
                )
        except httpx.RequestError as exc:
            raise ApiError(
                f"Cannot reach the kepler.gl server at {self.settings.server_url} — "
                f"{exc}. Start it with `npm run dev` in kepler-plugin-server, or point "
                f"KEPLER_GL_SERVER_URL at a deployment."
            ) from exc

        if response.status_code == 401 and retry_on_401:
            # The token was rejected. Mint a new one and try exactly once more:
            # if the second attempt is also refused, the problem is not expiry
            # and repeating it would only hide that.
            try:
                self.tokens.get_token(force_refresh=True)
            except AuthError as exc:
                raise ApiError(
                    f"The server rejected the token and a new one could not be "
                    f"obtained — {exc}"
                ) from exc
            return self._request(
                method,
                path,
                json_body=json_body,
                timeout=timeout,
                retry_on_401=False,
            )

        if not response.is_success:
            message, code = self._explain(response)
            raise ApiError(message, code)

        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(
                f"{method} {path} returned {response.status_code} with a body that is "
                f"not JSON. Is KEPLER_GL_SERVER_URL pointing at the app rather than at "
                f"something else on that port?"
            ) from exc

    @staticmethod
    def _explain(response: httpx.Response) -> tuple[str, str | None]:
        """Turn an error response into a sentence that names the fix, and a code.

        The code is the server's own, passed through untouched. It is what lets
        a caller tell one 409 from another — a slug that is taken and an upload
        that never arrived are the same status and want opposite responses —
        without reading the message text.
        """
        detail = ""
        code: str | None = None
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = payload.get("error") or payload.get("message") or ""
                raw_code = payload.get("code")
                code = raw_code if isinstance(raw_code, str) and raw_code else None
            if not detail:
                detail = response.text[:300]
        except ValueError:
            detail = response.text[:300].strip()

        status = response.status_code
        # A code the server sent wins over the status's general hint: the
        # specific one is written for this exact failure.
        if code == "upload_missing":
            hint = " — the bytes never reached storage; the plugin will send them again."
        elif code == "payment_required":
            hint = (
                " — this account is over its free allowance and has no card on file. "
                "Add one in the server's billing page, or delete something."
            )
        else:
            hint = {
                401: " — run the `login` tool, or check that the Auth0 audience matches.",
                402: (
                    " — this account is over its free allowance and has no card on "
                    "file. Add one in the server's billing page, or delete something."
                ),
                403: " — this account may not do that.",
                404: " — the map, project or dataset no longer exists on the server.",
                409: " — something with that name already exists.",
                413: " — the request body was too large for the server to accept.",
            }.get(status, "")

        prefix = {
            401: "Not authorised",
            402: "Payment required",
            403: "Forbidden",
            404: "Not found",
            409: "Conflict",
            413: "Too large",
        }.get(status, f"HTTP {status}")

        return f"{prefix}{hint}" + (f": {detail}" if detail else ""), code

    # -- identity ----------------------------------------------------------

    def me(self) -> dict:
        return self._request("GET", "/api/me") or {}

    # -- projects ----------------------------------------------------------

    def list_projects(self) -> list[RemoteProject]:
        payload = self._request("GET", "/api/projects") or {}
        rows = payload.get("projects", payload) if isinstance(payload, dict) else payload
        return [RemoteProject.from_json(row) for row in rows or []]

    def create_project(self, name: str) -> RemoteProject:
        payload = self._request("POST", "/api/projects", json_body={"name": name}) or {}
        return RemoteProject.from_json(payload.get("project", payload))

    # -- maps --------------------------------------------------------------

    def list_maps(self, project_id: str | None = None) -> list[RemoteMap]:
        path = (
            f"/api/projects/{project_id}/maps" if project_id else "/api/maps"
        )
        payload = self._request("GET", path) or {}
        rows = payload.get("maps", payload) if isinstance(payload, dict) else payload
        return [RemoteMap.from_json(row) for row in rows or []]

    def get_map(self, map_id: str) -> dict:
        payload = self._request("GET", f"/api/maps/{map_id}") or {}
        return payload.get("map", payload)

    def create_map(
        self,
        *,
        title: str,
        project_id: str | None,
        config: Any,
        description: str | None = None,
        datasets: list[dict[str, Any]] | None = None,
    ) -> dict:
        """Create a map, optionally with datasets already uploaded.

        `datasets` entries carry the id the upload returned plus what the map
        needs to draw it — the table name the config's layers reference, a
        label, the kind, and the coordinate columns. The server stores that and
        renders a page whose datasets point at
        `/api/datasets/<id>/download`, so the bytes are served through the app
        and can be counted.
        """
        body: dict[str, Any] = {"title": title, "config": config}
        if project_id:
            body["projectId"] = project_id
        if description:
            body["description"] = description
        if datasets:
            body["datasets"] = datasets
        payload = self._request("POST", "/api/maps", json_body=body) or {}
        return payload.get("map", payload)

    def update_map(
        self,
        map_id: str,
        *,
        config: Any = None,
        title: str | None = None,
        description: str | None = None,
        datasets: list[dict[str, Any]] | None = None,
    ) -> dict:
        """PATCH a map. Only the fields given are sent.

        The server merges what arrives rather than replacing the row, which is
        what makes saving a config from the viewer safe: the viewer knows the
        config and nothing else, and a full replace would blank the title.

        `datasets`, when given, *is* a replacement rather than a merge — a
        dataset the local map no longer has should not linger on the server
        being counted against the account.
        """
        body: dict[str, Any] = {}
        if config is not None:
            body["config"] = config
        if title is not None:
            body["title"] = title
        if description is not None:
            body["description"] = description
        if datasets is not None:
            body["datasets"] = datasets
        if not body:
            raise ApiError("update_map called with nothing to update.")
        payload = self._request("PATCH", f"/api/maps/{map_id}", json_body=body) or {}
        return payload.get("map", payload)

    def delete_map(self, map_id: str) -> None:
        self._request("DELETE", f"/api/maps/{map_id}")

    # -- datasets ----------------------------------------------------------

    def upload_dataset(
        self,
        payload: bytes,
        *,
        table: str,
        label: str | None = None,
        kind: str | None = None,
        map_id: str | None = None,
        row_count: int | None = None,
    ) -> dict:
        """Store a Parquet dataset: reserve a row, PUT the bytes, then commit.

        Three calls rather than one, and the shape is the platform's rather than
        a preference. Vercel refuses a function request body over 4.5 MB, and a
        Parquet dataset is routinely a hundred times that, so the bytes cannot
        come through the app at all: the app writes the row and hands back a
        presigned URL, the bytes go straight to S3, and the commit tells the app
        to look. The alternative — POSTing the file as multipart — works against
        `next dev` and fails on the first real deployment, which is the worst
        way for it to fail. There is one path, and it is the deployed one.

        The bytes are converted to Parquet before they get here; the server
        never sees a CSV. That conversion needs DuckDB, which is already a
        dependency of this plugin, and doing it here means what is stored is
        exactly what the viewer will read back.

        Args:
            payload: The Parquet file's bytes.
            table: The name the map's config references this dataset by. It is
                what the server stores as `table`, and renaming it would rename
                it out from under every layer that draws it.
            label: What a person sees in a listing. Defaults to `table`.
            kind: One of `point`, `geojson`, `h3`, `table` — how kepler should
                read it. The server refuses anything else rather than guessing,
                because a wrong kind draws a blank map with no message.
            map_id: Attach to an existing map in the same call. Usually omitted:
                the plugin uploads the data first and posts the map once it has
                the ids.
            row_count: What DuckDB counted. Sent on the commit, because the
                server cannot count rows in a file it never reads.

        Returns the server's dataset record, including the `id` the map's
        `datasets` payload needs.
        """
        if not payload:
            raise ApiError(
                "Refusing to upload an empty dataset — the server would store a "
                "zero-byte object and the viewer would draw nothing from it."
            )

        body: dict[str, Any] = {"table": table, "kind": kind or "table"}
        if label:
            body["label"] = label
        if map_id:
            body["mapId"] = map_id
        if row_count is not None:
            body["rowCount"] = row_count

        reserved = self._request("POST", "/api/datasets", json_body=body) or {}
        dataset = reserved.get("dataset", reserved)
        dataset_id = _as_str(dataset.get("id"))
        upload_url = dataset.get("uploadUrl")
        if not dataset_id or not upload_url:
            raise ApiError(
                "The server reserved a dataset but returned no upload URL, so the "
                "bytes have nowhere to go. This is a server bug rather than a "
                "problem with the map."
            )

        # Twice at most. The commit looks for the object, and an object that is
        # not there means the PUT did not land — a dropped connection, a proxy
        # that truncated it. Sending it again is the right response, and doing
        # it more than once is not: a second failure is not a race.
        for attempt in (1, 2):
            self._put_bytes(upload_url, payload)
            try:
                committed = (
                    self._request(
                        "POST",
                        f"/api/datasets/{dataset_id}/commit",
                        json_body={"rowCount": row_count} if row_count is not None else {},
                        timeout=UPLOAD_TIMEOUT,
                    )
                    or {}
                )
            except ApiError as exc:
                if exc.code == "upload_missing" and attempt == 1:
                    continue
                raise
            return committed.get("dataset", committed)

        raise ApiError(  # pragma: no cover - the loop always returns or raises
            "The dataset's bytes could not be confirmed after two attempts."
        )

    def _put_bytes(self, url: str, payload: bytes) -> None:
        """PUT the Parquet bytes to the presigned URL.

        Deliberately not `_request`: this URL points at the bucket, not at the
        app, and it carries its own credential in the query string. Sending an
        `Authorization` header alongside those query parameters makes S3 try
        header-based SigV4 instead, and fail — so the one header that must not
        be here is the one every other call in this class adds.

        `Content-Type` is safe because the server signs the URL with `host` as
        its only signed header: an unsigned header is not compared against the
        signature, and the object is stored with the type we name rather than
        the default. That is also why the server signs no content type — it
        would oblige this side to produce a string AWS agrees with, and
        `mimetypes` disagrees about `.parquet` between macOS and Linux.
        """
        try:
            with httpx.Client(timeout=UPLOAD_TIMEOUT) as client:
                response = client.put(
                    url,
                    content=payload,
                    headers={"Content-Type": PARQUET_CONTENT_TYPE},
                )
        except httpx.RequestError as exc:
            raise ApiError(
                f"The dataset could not be sent to storage — {exc}. The app "
                f"reserved a row for it; that row is deleted the next day if the "
                f"bytes never arrive."
            ) from exc

        if not response.is_success:
            detail = response.text[:300].strip()
            raise ApiError(
                f"Storage refused the upload with HTTP {response.status_code}"
                + (f": {detail}" if detail else "")
                + ". The presigned URL may have expired — call the tool again to "
                "get a fresh one."
            )

    def dataset_url(self, dataset_id: str) -> str:
        """The absolute URL the viewer should read a dataset's rows from.

        It points at the app, not at S3, and that is the whole reason egress can
        be metered. The viewer reads Parquet in byte ranges — a header, a footer,
        a column chunk — so the app proxies those ranges and counts what it
        forwards. Handing the browser a presigned URL instead would be faster
        and would make every read invisible: the object would be fetched from S3
        directly and the app would never learn it happened, so "we charge actual
        networks" would be a claim with nothing behind it.
        """
        return self._url(f"/api/datasets/{dataset_id}/download")
