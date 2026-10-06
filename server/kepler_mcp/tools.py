"""The MCP tool surface.

The tools are thin: each resolves the session (settings, token provider, map
store, preview server) and turns its failures into text a model can act on. The
work lives in `data.py`, `layers.py`, `maps.py`, `store.py` and `preview.py`.

Five rules shape the responses.

  * **A map is a URL, and the URL is the point.** `create_map` and `open_map`
    publish the map on a loopback port and the reply leads with that URL. A
    `file://` page cannot save and cannot fetch its own Parquet, so handing back
    a path and calling it done produces a map that renders once and then cannot
    be edited — the difference between a screenshot and a tool. The path is in
    the reply too, because it is what the user shares and keeps.

  * **A local map needs no account and no network.** The whole local half —
    load a CSV, build layers, write a page, open it — is filesystem work. It
    must not be gated behind `login`, and the error text when something hosted
    is attempted without a token says so rather than implying the plugin is
    broken.

  * **The reader is a model, not a person.** `inspect_data` prints a schema and
    a handful of sample rows, never the dataset. A file of a million rows must
    not arrive in the conversation.

  * **Errors are answers.** A failed call returns a sentence naming the fix —
    which columns the file actually has, that the port is taken, that this is
    an Auth0 setting — because a tool error an agent cannot read is a dead end.

  * **Destruction is confirmed.** Deleting a map removes work the user made and
    nothing here can undo it, so `delete_map` and `delete_server_map` ask first
    through MCP elicitation, and refuse without it unless the caller passed
    `confirm=True` explicitly.

Two mechanics worth knowing before editing this module.

  * **Sync tools run on a worker thread** (`anyio.to_thread.run_sync`), so the
    only route back to the request's event loop is `anyio.from_thread.run`. That
    is how progress is reported, and why a failure there is swallowed: a
    notification must never be the reason a tool call fails.

  * **Async tools run on the loop**, so anything blocking in one has to be
    offloaded. Only the two delete tools are async, and only because
    `Context.elicit` is.
"""

from __future__ import annotations

import functools
import json
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

import anyio
from mcp.server.elicitation import CancelledElicitation, DeclinedElicitation
from mcp.server.mcpserver import Context
from pydantic import BaseModel, Field, create_model

from .api import ApiClient, ApiError
from .auth import AuthError, TokenProvider
from .config import Settings
from .data import (
    GEOJSON_COLUMN,
    INLINE_BYTE_CAP,
    INLINE_ROW_CAP,
    DataError,
    LoadedTable,
    Session,
    summarise,
)
from .layers import LayerOptions, build_layer, describe_layers
from .maps import build_spec, config_for, dataset_spec, label_for
from .preview import server_for
from .store import LocalMap, LocalStore, StoreError, slugify

#: Sample rows printed by `inspect_data`, and rows shown per dataset in a
#: report. Enough to see what the data looks like; nothing close to a payload.
MAX_SAMPLE_ROWS = 8

#: The inline ceilings, restated in the reply when they are hit. Imported rather
#: than written out, so the message cannot drift from the behaviour.
INLINE_NOTE = (
    f"rows are inlined into the page below {INLINE_ROW_CAP:,} rows and "
    f"{INLINE_BYTE_CAP // (1024 * 1024)} MB; past either, the data goes to "
    f"`data/*.parquet` and the map needs the preview URL to load it"
)


def _guard(fn: Callable[..., str]) -> Callable[..., str]:
    """Turn a raised failure into a readable tool result.

    `functools.wraps` keeps the signature — and therefore the JSON schema the
    MCP server derives from the annotations — pointing at the undecorated
    function.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except (ApiError, AuthError, DataError, StoreError) as exc:
            return f"ERROR: {exc}"
        except Exception as exc:  # noqa: BLE001 - this is the tool boundary
            return f"ERROR: {type(exc).__name__}: {exc}"

    return wrapper


def _guard_async(fn: Callable[..., Any]) -> Callable[..., Any]:
    """`_guard` for a coroutine function, with the same reasoning."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await fn(*args, **kwargs)
        except (ApiError, AuthError, DataError, StoreError) as exc:
            return f"ERROR: {exc}"
        except Exception as exc:  # noqa: BLE001 - this is the tool boundary
            return f"ERROR: {type(exc).__name__}: {exc}"

    return wrapper


def _notify(ctx: Context | None, method: str, *args: Any) -> None:
    """Fire a progress notification from a sync tool, or give up quietly.

    A sync tool runs on an anyio worker thread, so the call has to hop back to
    the request's loop and fails outright if the tool was invoked outside one —
    as a test script does. A client that did not ask for progress makes it a
    documented no-op, and a client that dropped the connection raises into here.
    None of those are reasons for a tool call to fail.
    """
    if ctx is None:
        return
    try:
        anyio.from_thread.run(getattr(ctx, method), *args)
    except Exception:  # noqa: BLE001 - see above
        pass


def _can_elicit(ctx: Context | None) -> bool:
    """Whether the client advertised elicitation.

    Checked before eliciting, because nothing in the transport does it: the
    request goes out either way, and a client that does not implement it answers
    with "method not found", which surfaces as a tool error the user has to
    interpret.
    """
    if ctx is None:
        return False
    try:
        return getattr(ctx.client_capabilities, "elicitation", None) is not None
    except Exception:  # noqa: BLE001 - no request context, so no capabilities
        return False


class NoTitleModel(BaseModel):
    """A model whose rendered schema carries no top-level `title`.

    Codex's typed-schema parser accepts exactly `$schema`, `type`, `properties`
    and `required`; anything else is a parse error, and a parse error is answered
    as a *cancellation* — the card is never drawn, nobody is asked, and the
    refusal is indistinguishable from a user clicking No. Pydantic puts the
    model's own name in that top-level `title`, which is what trips it.
    """

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: Any, handler: Any
    ) -> dict[str, Any]:
        schema = handler(core_schema)
        schema.pop("title", None)
        return schema


def confirm_schema(subject: str, consequence: str) -> type[BaseModel]:
    """A yes/no card, as a two-value enum rather than a boolean.

    An enum is what every client renders as a pair of buttons; a boolean renders
    as a checkbox, and a checkbox that defaults to false makes the affirmative
    answer a thing the user has to *find* rather than choose.
    """
    return create_model(
        "ConfirmDelete",
        __base__=NoTitleModel,
        choice=(
            Literal["delete", "cancel"],
            Field(description=f"delete {subject}? {consequence}"),
        ),
    )


def _markdown_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    """A markdown table, hand-rolled to keep a table dependency out."""
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        cells = []
        for column in columns:
            text = "" if row.get(column) is None else str(row[column])
            if len(text) > 60:
                text = text[:57] + "…"
            cells.append(text.replace("|", "\\|").replace("\n", " "))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _sources(data: str | list[str]) -> list[str]:
    """Normalise the `data` argument to a list.

    A single string is accepted because it is what a caller with one file
    writes, and a validation error listing the expected array is a poor way to
    learn that.
    """
    if isinstance(data, str):
        return [data] if data.strip() else []
    return [str(item) for item in (data or []) if str(item).strip()]


@dataclass
class Prepared:
    """A session with its sources loaded and layers built."""

    session: Session
    tables: list[LoadedTable]
    layers: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)


class KeplerApp:
    """Session-scoped state shared by every tool call in one server process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tokens = TokenProvider(settings)
        self.api = ApiClient(settings, self.tokens)
        self.store = LocalStore(settings.maps_dir)

    # -- registration ------------------------------------------------------

    def register(self, server: Any) -> None:
        server.tool(title="Check sign-in status")(self.auth_status)
        server.tool(title="Sign in to the kepler.gl server")(self.login)
        server.tool(title="Sign out")(self.logout)
        server.tool(title="Inspect a data file")(self.inspect_data)
        server.tool(title="Create a kepler.gl map")(self.create_map)
        server.tool(title="List local maps")(self.list_maps)
        server.tool(title="Open a local map")(self.open_map)
        server.tool(title="Delete a local map")(self.delete_map)
        server.tool(title="List server projects")(self.list_projects)
        server.tool(title="Create a server project")(self.create_project)
        server.tool(title="List maps on the server")(self.list_server_maps)
        server.tool(title="Upload a map to the server")(self.upload_map)
        server.tool(title="Update a map on the server")(self.update_server_map)
        server.tool(title="Delete a map on the server")(self.delete_server_map)
        server.tool(title="Open a map on the server")(self.open_server_map)

    # -- authentication ----------------------------------------------------

    @_guard
    def auth_status(self) -> str:
        """Report whether a kepler.gl account is connected.

        Call this first when a hosted call fails on credentials. It makes no
        network request and never opens a browser — asking *about* the sign-in
        state must not itself start one.
        """
        status = self.tokens.status()
        lines = [
            f"Auth mode: {status['mode']}",
            f"Server: {status['server_url']}",
            f"Token cache: {status['token_path']}",
            f"Access token on file: {'yes' if status['has_cached_token'] else 'no'}",
            f"Refresh token on file: {'yes' if status['has_refresh_token'] else 'no'}",
        ]
        if status["signed_in_as"]:
            lines.append(f"Signed in as: {status['signed_in_as']}")
        if status["mode"] == "anonymous":
            lines.append(
                "No Auth0 application is configured, so this install is local-only: "
                "maps can be created, listed and opened, but not uploaded. Set "
                "KEPLER_GL_AUTH0_DOMAIN, KEPLER_GL_AUTH0_CLIENT_ID and "
                "KEPLER_GL_AUTH0_AUDIENCE to enable sign-in."
            )
        elif status["mode"] == "misconfigured":
            lines.append(
                "KEPLER_GL_DEV_TOKEN is set without KEPLER_GL_DEV_MODE=1, so it is "
                "ignored. Set KEPLER_GL_DEV_MODE=1, or unset the token."
            )
        elif status["mode"] == "pinned-dev-token":
            lines.append(
                "A pinned development token is in use, so no sign-in will happen "
                "and the account is whatever that token belongs to."
            )
        elif status["mode"] == "oauth" and not status["has_cached_token"]:
            lines.append("Not signed in yet. Call `login` to open a browser window.")

        maps = self.store.list_maps()
        lines.append("")
        lines.append(f"Local maps: {len(maps)} in {self.store.root}")
        if not self.settings.viewer_available:
            lines.append(self._bundle_warning())
        return "\n".join(lines)

    @_guard
    def login(self) -> str:
        """Sign in to the kepler.gl server, opening a browser window.

        Takes a few seconds and needs the user to finish in the browser; run it
        only when a hosted call has failed on credentials, or when the user asks
        to connect an account. Creating local maps needs no sign-in at all.
        """
        token = self.tokens.get_token(force_refresh=True)
        # The token itself is never printed. It is a bearer credential for the
        # user's account and this text goes into a conversation transcript.
        account = self.tokens.status().get("signed_in_as")
        who = self.api.me()
        lines = ["Signed in to the kepler.gl server."]
        if account:
            lines.append(f"Account: {account}")
        for key in ("email", "name", "plan", "mapCount", "map_count"):
            if who.get(key) is not None:
                lines.append(f"{key}: {who[key]}")
        lines.append(f"Server: {self.settings.server_url}")
        lines.append(f"Token cached at {self.settings.token_path} (mode 0600).")
        assert token  # nosec - get_token raises rather than returning empty
        return "\n".join(lines)

    @_guard
    def logout(self) -> str:
        """Forget the cached kepler.gl credentials.

        Local maps are unaffected. Hosted calls will need `login` again.
        """
        self.tokens.clear()
        return (
            "Signed out: the cached access and refresh tokens were deleted. "
            "Local maps in "
            f"{self.store.root} are untouched."
        )

    # -- local data --------------------------------------------------------

    @_guard
    def inspect_data(
        self,
        data: str | list[str],
        options: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        """Load a data file and report what it is, without creating a map.

        Call this before `create_map` when the data is unfamiliar: it returns
        the columns and their types, which of them the plugin takes as the
        geometry, which layer type that implies, and a few sample rows. Every
        argument `create_map` accepts in `options` can be rehearsed here for
        free — nothing is written to disk.

        Supported: CSV, TSV, Parquet, GeoJSON, JSON, newline-delimited JSON, and
        shapefile/GeoPackage where DuckDB's spatial extension is available. A
        path or an http(s) URL.

        Args:
            data: One path or URL, or a list of them.
            options: Per-source overrides, keyed by the exact string given in
                `data`. Recognised keys: `kind` (point | geojson | h3 | table),
                `lat`, `lng`, `layer_type`, `color_field`, `size_field`,
                `opacity`, `radius`, `label`. Use `"*"` as a key to apply a
                default to every source.
        """
        sources = _sources(data)
        if not sources:
            return "ERROR: No data source given."

        options = options or {}
        lines: list[str] = []
        with Session() as session:
            for index, source in enumerate(sources):
                entry = {**options.get("*", {}), **options.get(source, {})}
                loaded = session.load(
                    source,
                    # The same naming `create_map` uses, so the table name in
                    # this report is the one the layer will reference.
                    name=entry.get("label") or label_for(source),
                    kind=entry.get("kind"),
                    lat=entry.get("lat"),
                    lng=entry.get("lng"),
                )
                lines.append(self._describe_loaded(loaded, entry, index))
                lines.append("")
                lines.append(summarise(session.sample(loaded.table, MAX_SAMPLE_ROWS)))
                lines.append("")
        lines.append(
            f"Note: {INLINE_NOTE}. Run `create_map` with the same sources to build "
            f"the map."
        )
        return "\n".join(lines)

    def _describe_loaded(
        self, loaded: LoadedTable, entry: dict[str, Any], index: int
    ) -> str:
        lines = [f"### {label_for(loaded.source)}  ·  `{loaded.table}`"]
        lines.append(
            f"{loaded.row_count:,} rows · kind **{loaded.kind}**"
            + (f" · {loaded.geometry_note}" if loaded.geometry_note else "")
        )
        if loaded.kind == "point":
            lines.append(
                f"Coordinates: `{loaded.lat_column}`, `{loaded.lng_column}` — a point "
                f"layer will be built on them."
            )
        elif loaded.kind == "geojson":
            lines.append(
                f"Geometry column: `{GEOJSON_COLUMN}` — a geojson layer will be built "
                f"on it."
            )
        elif loaded.kind == "table":
            lines.append(
                "No geometry found, so no layer will be created. If there are "
                "coordinate columns the plugin missed, name them with "
                "`lat`/`lng`; a KML or WKT column needs the spatial extension."
            )
        rows = [
            {
                "column": column.name,
                "type": column.type,
                "sample": "",
            }
            for column in loaded.columns[:40]
        ]
        lines.append(_markdown_table(rows, ["column", "type", "sample"]).split("\n")[0])
        lines.append(
            "Columns: "
            + ", ".join(f"`{c.name}` {c.type}" for c in loaded.columns[:40])
            + (f", … {len(loaded.columns) - 40} more" if len(loaded.columns) > 40 else "")
        )
        for warning in loaded.warnings:
            lines.append(f"Warning: {warning}")
        # Any override the caller passed but the load did not need is worth
        # flagging: it usually means a typo in a column name that would
        # otherwise fail later, on the map.
        for key in ("lat", "lng", "color_field", "size_field"):
            if entry.get(key):
                lines.append(f"Applied: {key} = {entry[key]!r}")
        return "\n".join(lines)

    # -- local maps --------------------------------------------------------

    @_guard
    def create_map(
        self,
        data: str | list[str],
        title: str,
        description: str = "",
        options: dict[str, dict[str, Any]] | None = None,
        layer_type: str = "",
        lat: str = "",
        lng: str = "",
        style: str = "dark",
        latitude: float | None = None,
        longitude: float | None = None,
        zoom: float | None = None,
        slug: str = "",
        open_preview: bool = True,
        ctx: Context = None,
    ) -> str:
        """Create a kepler.gl map from one or more data files, as a local page.

        The map is a directory under the plugin's maps folder holding `map.html`,
        `spec.json` and any data too large to inline. The reply carries both the
        path and a loopback URL; **give the user the URL**, because a `file://`
        page cannot save its edits or fetch its own Parquet, while the loopback
        one can do both.

        No account is needed for any of this. Uploading is a separate tool.

        Args:
            data: One path or URL, or a list of them — the datasets to draw.
            title: What the map is called. Also names the directory.
            description: Optional longer text, stored in the map.
            options: Per-source overrides keyed by the string given in `data`,
                with `"*"` as a default for all. Keys: `kind` (point | geojson |
                h3 | table), `lat`, `lng`, `layer_type` (point, heatmap, grid,
                hexbin, cluster, geojson, 3d, hexagonId, arc, line), `label`,
                `color_field`, `size_field`, `color_scale`, `size_scale`,
                `opacity`, `radius`, `visible`, and for arc/line layers
                `source_lat`, `source_lng`, `target_lat`, `target_lng`.
            layer_type: A layer type applied to every dataset that does not name
                its own in `options`. Convenience for the common single-dataset
                call — `layer_type="heatmap"`.
            lat: Latitude column, applied to every dataset that does not name
                its own. Same convenience as `layer_type`.
            lng: Longitude column, likewise.
            style: `dark` (default) or `light`.
            latitude: Centre latitude, if the map should not fit itself to the
                data.
            longitude: Centre longitude, likewise.
            zoom: Zoom level, likewise. Supplying any of these three stops
                kepler from fitting the view to the data.
            slug: The directory name. Derived from the title when omitted; a
                numeric suffix is added rather than overwriting an existing map.
            open_preview: Start the loopback server and return a URL. Leave it
                on unless the user only wants the file.
        """
        sources = _sources(data)
        if not sources:
            return "ERROR: No data source given."
        if not title.strip():
            return "ERROR: A title is needed — it names the map's directory."

        options = options or {}
        prepared = self._prepare(sources, options, layer_type, lat, lng)
        try:
            map_slug = self.store.available_slug(slug.strip() or title)

            datasets = self._materialise_datasets(map_slug, prepared)
            config = config_for(prepared.tables, layers=prepared.layers, style=style)
            spec = build_spec(
                map_id=map_slug,
                title=title.strip(),
                description=description.strip() or None,
                datasets=datasets,
                config=config,
                centre_map=latitude is None and longitude is None and zoom is None,
                theme=style,
                # The save target is filled in below, once the server is up. It
                # is not part of what is written to disk from here on — see
                # `_save_target`, which reads it back off the running server so
                # a restarted process does not leave a stale token in a page.
                save=None,
                notes=self._notes(prepared),
            )
            record = self.store.write_map(map_slug, spec)
        finally:
            prepared.session.close()

        return self._report(
            record,
            prepared,
            heading=f"Created **{title.strip()}**",
            open_preview=open_preview,
            latitude=latitude,
            longitude=longitude,
            zoom=zoom,
        )

    @_guard
    def list_maps(self) -> str:
        """List the kepler.gl maps on this machine.

        Each map is a directory holding a page you can open and edit. Sorted by
        when each was last written, newest first.

        The reply also carries the URL of the **map index** — every local map on
        one page, a card each with a drawing of its data — which is the form to
        give a user who wants to look at their maps rather than read a table of
        them. It needs no account, and it is rendered from a fresh scan of the
        maps directory, so a map deleted or added outside the plugin shows up
        the moment the page is reloaded.
        """
        maps = self.store.list_maps()
        if not maps:
            return (
                f"No maps yet in {self.store.root}. `create_map` makes one from a "
                f"CSV, a Parquet file or a GeoJSON file."
            )
        rows = [
            {
                "title": record.title,
                "directory": record.slug,
                "datasets": len(record.datasets),
                "edited": _when(record),
                "description": record.description or "",
            }
            for record in maps
        ]
        lines = [
            f"{len(maps)} map(s) in {self.store.root}, newest first.",
            "",
            _markdown_table(
                rows, ["title", "directory", "datasets", "edited", "description"]
            ),
            "",
        ]
        index_url = self._index_url()
        if index_url:
            lines.append(f"All of them as one page, with previews: {index_url}")
        lines.append("Call `open_map` with a directory name to open a single map.")
        if not self.settings.viewer_available:
            lines.append("")
            lines.append(self._bundle_warning())
        return "\n".join(lines)

    def _index_url(self) -> str:
        """The map index's URL, or `""` when the preview server will not start.

        Listing is a read, and starting a server is a side effect a read would
        not otherwise have. It is worth having here — the index is the point of
        listing for a person — but not worth failing over, so a port that cannot
        be bound costs the reply a line rather than the whole call. `open_map`
        reports the same failure with its cause, which is where it is actionable.
        """
        try:
            self.store.ensure_bundle(
                self.settings.viewer_bundle, self.settings.viewer_version
            )
            return server_for(self.store, port=self.settings.preview_port).index_url
        except (StoreError, OSError):
            return ""

    @_guard
    def open_map(self, slug: str, open_browser: bool = False) -> str:
        """Open a saved local map and return a URL for it.

        Re-renders the page first, so a map created by an older version of the
        viewer picks up the current one.

        Args:
            slug: The map's directory name, as `list_maps` reports it.
            open_browser: Also open it in the system browser. Off by default:
                the URL is for the client's own preview pane, and a system
                browser window lands on top of the answer.
        """
        record = self.store.get(slug)
        self.store.ensure_bundle(self.settings.viewer_bundle, self.settings.viewer_version)
        spec = self._save_target(record.spec, slug)
        record = self.store.write_map(slug, spec)

        url = server_for(self.store, port=self.settings.preview_port).map_url(slug)
        if open_browser:
            webbrowser.open(url)
        return "\n".join(
            [
                f"**{record.title}**",
                "",
                f"Open: {url}",
                f"File: {record.html_path}",
                "",
                self._dataset_summary(record),
                "",
                "Edits made in the page are saved back to "
                f"`{record.spec_path}` by its Save button.",
            ]
        )

    @_guard_async
    async def delete_map(
        self, slug: str, confirm: bool = False, ctx: Context = None
    ) -> str:
        """Delete a local map and everything in its directory.

        Destructive and not undoable from here. Asks for confirmation unless
        `confirm=True` is passed, which is for a caller whose user has already
        said so in as many words.

        Args:
            slug: The map's directory name, as `list_maps` reports it.
            confirm: Skip the confirmation card. Only after the user has agreed
                to this specific map being deleted.
        """
        record = self.store.get(slug)
        if not confirm and not await self._confirmed(
            ctx,
            subject=f"the map {record.title!r} in {record.directory}",
            consequence=(
                f"Its {len(record.datasets)} dataset(s), its saved configuration "
                f"and its page are removed. This cannot be undone."
            ),
        ):
            return (
                f"Not deleted. {record.title!r} is still at {record.directory}. "
                f"Ask the user to confirm, then call again with confirm=True."
            )
        where = self.store.delete(slug)
        return f"Deleted {record.title!r} — removed {where}."

    # -- the local build ---------------------------------------------------

    def _prepare(
        self,
        sources: list[str],
        options: dict[str, dict[str, Any]],
        layer_type: str,
        lat: str,
        lng: str,
    ) -> Prepared:
        """Load every source and build every layer, or raise.

        One session for the whole map, because the layers reference tables by
        the names this session gave them and a second session would not know
        them.
        """
        session = Session()
        tables: list[LoadedTable] = []
        layers: list[dict[str, Any]] = []
        notes: list[str] = []

        try:
            for index, source in enumerate(sources):
                entry = {**options.get("*", {}), **options.get(source, {})}
                loaded = session.load(
                    source,
                    # Named from the file rather than left as `dataset_1`. The
                    # table name becomes the layer's `dataId`, the key in the
                    # tooltip field map, the Parquet filename and a column of
                    # every report this returns — and `dataset_1` in all four
                    # places is a map nobody can read.
                    name=entry.get("label") or label_for(source),
                    kind=entry.get("kind"),
                    lat=entry.get("lat") or lat or None,
                    lng=entry.get("lng") or lng or None,
                )
                if loaded.row_count == 0:
                    raise DataError(
                        f"{source} loaded with no rows. A map drawn from it would be "
                        f"an empty basemap, which reads as a bug — check the file, or "
                        f"the delimiter and header settings it needs."
                    )
                tables.append(loaded)

                layer = build_layer(
                    loaded,
                    index=index,
                    options=LayerOptions(
                        layer_type=entry.get("layer_type") or layer_type or None,
                        label=entry.get("label") or label_for(loaded.source),
                        color_field=entry.get("color_field"),
                        size_field=entry.get("size_field"),
                        color_scale=entry.get("color_scale", "quantile"),
                        size_scale=entry.get("size_scale", "linear"),
                        opacity=entry.get("opacity"),
                        radius=entry.get("radius"),
                        visible=entry.get("visible", True),
                        source_lat=entry.get("source_lat"),
                        source_lng=entry.get("source_lng"),
                        target_lat=entry.get("target_lat"),
                        target_lng=entry.get("target_lng"),
                    ),
                )
                if layer is not None:
                    layers.append(layer)
                else:
                    notes.append(
                        f"{label_for(loaded.source)} has no geometry, so it was loaded "
                        f"without a layer — its columns are in the tooltip and the "
                        f"layer panel."
                    )
                notes.extend(loaded.warnings)
        except Exception:
            session.close()
            raise

        return Prepared(session=session, tables=tables, layers=layers, notes=notes)

    def _materialise_datasets(self, slug: str, prepared: Prepared) -> list[dict[str, Any]]:
        """Inline each table's rows, or write them to `data/*.parquet`.

        The choice is made per dataset and reported, because it is visible to
        the user: an inlined map opens from Finder and a Parquet-backed one does
        not. Nothing is decided silently.
        """
        datasets: list[dict[str, Any]] = []
        for loaded in prepared.tables:
            size = prepared.session.inline_size(loaded.table)
            if loaded.row_count <= INLINE_ROW_CAP and 0 < size <= INLINE_BYTE_CAP:
                rows = prepared.session.rows(loaded.table)
                datasets.append(dataset_spec(loaded, rows=rows))
                continue

            payload = prepared.session.to_parquet(loaded.table)
            filename = f"{loaded.table}.parquet"
            self.store.write_data(slug, filename, payload)
            datasets.append(
                dataset_spec(loaded, parquet_url=f"./data/{filename}")
            )
            reason = (
                f"{loaded.row_count:,} rows"
                if loaded.row_count > INLINE_ROW_CAP
                else f"{_size(size)} of JSON"
            )
            prepared.notes.append(
                f"{label_for(loaded.source)} is stored as `data/{filename}` "
                f"({reason}, past the inline limit), so it loads over http — the "
                f"preview URL, not the file."
            )
        return datasets

    def _notes(self, prepared: Prepared) -> list[str]:
        """Deduplicated notes, order preserved."""
        seen: set[str] = set()
        kept: list[str] = []
        for note in prepared.notes:
            if note not in seen:
                seen.add(note)
                kept.append(note)
        return kept

    def _save_target(self, spec: dict[str, Any], slug: str) -> dict[str, Any]:
        """Attach a live save target to a spec, and drop a stale one.

        Deliberately *not* part of what `create_map` writes to disk. The target
        carries the running preview server's token, and a token written into a
        file outlives the process that minted it — a `spec.json` written today
        would carry yesterday's token, and the page would show a Save button
        that fails with "not open to this page" for reasons nothing on screen
        explains. It is attached as the page is rendered instead.

        The `file://` half of this is handled in the page itself, which deletes
        the target when the protocol is `file:` — the same page has to work both
        ways and only the browser knows which one it is.
        """
        spec = dict(spec)
        server = server_for(self.store, port=self.settings.preview_port)
        spec["save"] = server.save_target(slug)
        return spec

    def _report(
        self,
        record: LocalMap,
        prepared: Prepared,
        *,
        heading: str,
        open_preview: bool,
        latitude: float | None,
        longitude: float | None,
        zoom: float | None,
    ) -> str:
        """The reply to `create_map`: what was made, where it is, how to open it."""
        lines = [heading + ".", ""]

        missing = self.store.ensure_bundle(
            self.settings.viewer_bundle, self.settings.viewer_version
        )
        if missing is None:
            lines.append(self._bundle_warning())
            lines.append("")

        if any(value is not None for value in (latitude, longitude, zoom)):
            # The config was built with a default viewport; the caller's centre
            # goes in as a spec-level override of kepler's own fit.
            spec = dict(record.spec)
            config = dict(spec.get("config") or {})
            inner = dict(config.get("config") or {})
            map_state = dict(inner.get("mapState") or {})
            if latitude is not None:
                map_state["latitude"] = latitude
            if longitude is not None:
                map_state["longitude"] = longitude
            if zoom is not None:
                map_state["zoom"] = zoom
            inner["mapState"] = map_state
            config["config"] = inner
            spec["config"] = config
            record = self.store.write_map(record.slug, spec)

        if open_preview:
            spec = self._save_target(record.spec, record.slug)
            record = self.store.write_map(record.slug, spec)
            server = server_for(self.store, port=self.settings.preview_port)
            url = server.map_url(record.slug)
            lines.append(f"Open: {url}")
            # The index is free here — the server is already up — and it is
            # where the user goes to find this map again after the conversation
            # ends, which is the moment the URL above stops being at hand.
            lines.append(f"All your maps: {server.index_url}")
        else:
            lines.append(f"Open: {record.html_path}")

        lines.append(f"File: {record.html_path}")
        lines.append(f"Directory: {record.directory}")
        lines.append("")
        lines.append(self._dataset_summary(record))
        lines.append("")
        lines.append(f"Layers: {describe_layers(prepared.layers)}")
        if open_preview:
            lines.append("")
            lines.append(
                "The page's Save button writes edits back to "
                f"`{record.spec_path}`. Opening `map.html` directly from the "
                "filesystem also renders the map, but cannot save."
            )
        notes = self._notes(prepared)
        if notes:
            lines.append("")
            lines.extend(f"Note: {note}" for note in notes)
        return "\n".join(lines)

    def _dataset_summary(self, record: LocalMap) -> str:
        rows = []
        for dataset in record.datasets:
            source = (
                f"{len(dataset['rows']):,} inlined"
                if "rows" in dataset
                else f"parquet {dataset.get('parquetUrl', '')}"
            )
            rows.append(
                {
                    "dataset": dataset.get("label") or dataset.get("id"),
                    "kind": dataset.get("kind"),
                    "id": dataset.get("id"),
                    "rows": source,
                }
            )
        if not rows:
            return "No datasets."
        return _markdown_table(rows, ["dataset", "kind", "id", "rows"])

    def _bundle_warning(self) -> str:
        """What to say when the viewer bundle is not where it should be."""
        return (
            f"WARNING: the viewer bundle is missing from {self.settings.vendor_dir} "
            f"(expected `kepler-viewer.js`). Maps will render as an empty page until "
            f"it is built: run `npm install && npm run build` in the plugin's "
            f"`viewer` directory. Everything else — the data, the layers, the saved "
            f"config — is already written and will render once it is there."
        )

    # -- the hosted half ---------------------------------------------------

    @_guard
    def list_projects(self) -> str:
        """List the projects on the kepler.gl server.

        Maps are grouped by project, and a project is what a map is uploaded
        into. Requires sign-in.
        """
        projects = self.api.list_projects()
        if not projects:
            return (
                f"No projects on {self.settings.server_url} yet. `create_project` "
                f"makes one, or `upload_map` will make one for you."
            )
        rows = [
            {
                "name": project.name,
                "id": project.id,
                "slug": project.slug or "",
                "maps": project.map_count,
            }
            for project in projects
        ]
        return "\n".join(
            [
                f"{len(projects)} project(s) on {self.settings.server_url}.",
                "",
                _markdown_table(rows, ["name", "id", "slug", "maps"]),
                "",
                "`list_server_maps` with a project id lists what is in one.",
            ]
        )

    @_guard
    def create_project(self, name: str) -> str:
        """Create a project on the kepler.gl server to group maps under.

        Args:
            name: The project's name. Must not already exist.
        """
        project = self.api.create_project(name.strip())
        return (
            f"Created project **{project.name}** (id `{project.id}`) on "
            f"{self.settings.server_url}."
        )

    @_guard
    def list_server_maps(self, project_id: str = "") -> str:
        """List the maps stored on the kepler.gl server.

        Args:
            project_id: Show only this project's maps. Omit to list every map the
                account owns.
        """
        maps = self.api.list_maps(project_id or None)
        if not maps:
            where = f"project `{project_id}`" if project_id else "this account"
            return (
                f"No maps in {where} on {self.settings.server_url}. `upload_map` "
                f"takes a local map and puts it there."
            )
        rows = [
            {
                "title": record.title,
                "id": record.id,
                "project": record.project_id or "",
                "datasets": record.dataset_count,
                "updated": (record.updated_at or "")[:19].replace("T", " "),
            }
            for record in maps
        ]
        return "\n".join(
            [
                f"{len(maps)} map(s) on {self.settings.server_url}.",
                "",
                _markdown_table(rows, ["title", "id", "project", "datasets", "updated"]),
                "",
                "`open_server_map` with an id returns the URL to view and edit one.",
            ]
        )

    @_guard
    def open_server_map(self, map_id: str) -> str:
        """Return the URL of a map on the server, where it can be edited.

        The hosted page is the same viewer bundle as a local map's, so edits
        made there are saved to the server's database with the same Save button.

        Args:
            map_id: The map's id, as `list_server_maps` reports it.
        """
        record = self.api.get_map(map_id)
        title = record.get("title") or "(untitled)"
        url = record.get("url") or f"{self.settings.server_url}/maps/{map_id}"
        lines = [f"**{title}**", "", f"Open: {url}"]
        datasets = record.get("datasets") or []
        if datasets:
            lines.append("")
            lines.append(
                _markdown_table(
                    [
                        {
                            "dataset": d.get("label") or d.get("table"),
                            "kind": d.get("kind"),
                            "rows": d.get("rowCount") or d.get("row_count") or "",
                        }
                        for d in datasets
                    ],
                    ["dataset", "kind", "rows"],
                )
            )
        updated = record.get("updatedAt") or record.get("updated_at")
        if updated:
            lines.append("")
            lines.append(f"Last saved: {str(updated)[:19].replace('T', ' ')}")
        return "\n".join(lines)

    @_guard
    def upload_map(
        self,
        slug: str,
        project: str = "",
        title: str = "",
        description: str = "",
    ) -> str:
        """Upload a local map — its data and its configuration — to the server.

        The datasets are converted to Parquet locally and uploaded as they are
        stored; the configuration goes up as the saved kepler config. The result
        is a hosted map that opens at a URL and can be edited there, with each
        save written to the server.

        Requires sign-in. The local map is left exactly as it was.

        Args:
            slug: The local map's directory name, as `list_maps` reports it.
            project: The project to file it under, by name or id. Omitted, it
                goes to the account's default project — and the project is
                created if the name does not exist yet.
            title: Override the map's title on the server. Defaults to the local
                one.
            description: Override the description.
        """
        return self._push(
            self.store.get(slug),
            project=project.strip(),
            title=title.strip(),
            description=description.strip(),
            map_id="",
        )

    @_guard
    def update_server_map(
        self,
        map_id: str,
        slug: str,
        title: str = "",
        description: str = "",
    ) -> str:
        """Replace a server map's data and configuration with a local map's.

        Use this after a map has been edited locally and the server's copy
        should catch up. The server map keeps its id, its project and its URL;
        its datasets are replaced, and any dataset that is no longer part of the
        local map is removed from it.

        Args:
            map_id: The server map's id, as `list_server_maps` reports it.
            slug: The local map to push, by directory name.
            title: Also rename it on the server. Omitted, the title is left.
            description: Also replace the description.
        """
        return self._push(
            self.store.get(slug),
            project="",
            title=title.strip(),
            description=description.strip(),
            map_id=map_id.strip(),
        )

    def _push(
        self,
        record: LocalMap,
        *,
        project: str,
        title: str,
        description: str,
        map_id: str,
    ) -> str:
        """Upload a local map's datasets and config, creating or updating.

        The datasets are rebuilt from what the map *renders* rather than from
        the files it was originally made from. That matters: a map whose source
        CSV has since been edited would otherwise upload a version nobody has
        seen. What the page shows is what goes up.
        """
        if not self.settings.login_configured and not (
            self.settings.dev_token and self.settings.dev_mode
        ):
            raise AuthError(
                "Uploading needs a kepler.gl account, and this install has no Auth0 "
                "application configured. Set KEPLER_GL_AUTH0_DOMAIN, "
                "KEPLER_GL_AUTH0_CLIENT_ID and KEPLER_GL_AUTH0_AUDIENCE, then call "
                "`login`. Creating and editing local maps needs none of this."
            )

        uploads: list[dict[str, Any]] = []
        with Session() as session:
            for index, dataset in enumerate(record.datasets):
                table = dataset.get("id") or f"dataset_{index}"
                payload, row_count = self._dataset_parquet(session, record, dataset, table)
                uploads.append(
                    {
                        "table": table,
                        "label": dataset.get("label") or table,
                        "kind": dataset.get("kind") or "table",
                        "datasetId": self._upload_bytes(
                            payload, table, dataset, row_count
                        ),
                    }
                )

        project_id = self._resolve_project(project) if project else ""
        config = record.spec.get("config")

        if map_id:
            remote = self.api.update_map(
                map_id,
                config=config,
                title=title or None,
                description=description or None,
                datasets=uploads,
            )
        else:
            remote = self.api.create_map(
                title=title or record.title,
                project_id=project_id or None,
                config=config,
                description=description or record.description,
                datasets=uploads,
            )

        remote_id = str(remote.get("id") or map_id)
        url = remote.get("url") or f"{self.settings.server_url}/maps/{remote_id}"
        verb = "Updated" if map_id else "Uploaded"
        lines = [
            f"{verb} **{remote.get('title') or title or record.title}** on "
            f"{self.settings.server_url}.",
            "",
            f"Open: {url}",
            f"Map id: `{remote_id}`",
            "",
            _markdown_table(
                [
                    {
                        "dataset": item["label"],
                        "kind": item["kind"],
                        "stored as": item["datasetId"],
                    }
                    for item in uploads
                ],
                ["dataset", "kind", "stored as"],
            ),
            "",
            "The data is stored as Parquet and read back over the app's own "
            "download route, so the bytes served are counted against the account.",
        ]
        return "\n".join(lines)

    def _dataset_parquet(
        self,
        session: Session,
        record: LocalMap,
        dataset: dict[str, Any],
        table: str,
    ) -> tuple[bytes, int]:
        """The bytes to upload for one dataset of a local map, and its row count.

        Either a file already written beside the map, or the inlined rows turned
        back into Parquet — which is why the round trip is exact rather than
        approximate. Rebuilding from the original source file would be cheaper
        and wrong: the map may have been edited since, and its rows may have
        been filtered by the build.

        The count comes back with the bytes because the server cannot produce it:
        it never reads the file, only stores it, and a listing that shows a row
        count is more use than one that shows a blank.
        """
        url = dataset.get("parquetUrl")
        if url:
            path = (record.directory / url.lstrip("./")).resolve()
            if record.directory not in path.parents:
                raise StoreError(f"{url!r} points outside the map's directory.")
            if not path.exists():
                raise StoreError(
                    f"{record.slug} references {url}, which is not on disk. Re-create "
                    f"the map with `create_map`."
                )
            # Reading it back also proves it is Parquet at all — the server
            # stores whatever it is given, and a file the viewer cannot parse
            # would be a blank map with no explanation.
            return path.read_bytes(), session.count_parquet(path)

        rows = dataset.get("rows")
        if not rows:
            raise StoreError(
                f"Dataset {table!r} in {record.slug} has neither rows nor a Parquet "
                f"file, so there is nothing to upload."
            )
        # `to_parquet` writes through a real file, so this is a genuine Parquet
        # round trip rather than a JSON body with a Parquet content type.
        session.from_rows(table, rows)
        return session.to_parquet(table), len(rows)

    def _upload_bytes(
        self, payload: bytes, table: str, dataset: dict[str, Any], row_count: int
    ) -> str:
        """Put one dataset's Parquet bytes on the server, and return its id.

        No temporary file any more: the bytes go straight to S3 with a PUT, so
        there is nothing for httpx to encode and nothing holding the user's data
        on disk while it happens.
        """
        uploaded = self.api.upload_dataset(
            payload,
            # The name the config's layers already reference by `dataId`. The
            # server stores it verbatim; renaming it here would rename it out
            # from under every layer that draws this dataset.
            table=table,
            label=dataset.get("label"),
            kind=dataset.get("kind"),
            row_count=row_count,
        )
        dataset_id = uploaded.get("id")
        if not dataset_id:
            raise ApiError(
                "The server accepted the dataset but returned no id, so the map "
                "could not reference it."
            )
        return str(dataset_id)

    def _resolve_project(self, project: str) -> str:
        """A project id from a name or an id, creating the project if needed.

        Creating on demand because the alternative is a two-step dance — list,
        notice it is missing, create, upload — that a caller has to be told
        about and will get wrong once.
        """
        for existing in self.api.list_projects():
            if project in {existing.id, existing.name, existing.slug}:
                return existing.id
        return self.api.create_project(project).id

    @_guard_async
    async def delete_server_map(
        self, map_id: str, confirm: bool = False, ctx: Context = None
    ) -> str:
        """Delete a map from the server, including its stored data.

        Destructive and not undoable from here. The local map, if there is one,
        is untouched. Asks for confirmation unless `confirm=True` is passed.

        Args:
            map_id: The map's id, as `list_server_maps` reports it.
            confirm: Skip the confirmation card, only after the user has agreed
                to this specific map being deleted.
        """
        record = self.api.get_map(map_id)
        title = record.get("title") or map_id
        if not confirm and not await self._confirmed(
            ctx,
            subject=f"the server map {title!r} (id `{map_id}`)",
            consequence=(
                "Its configuration and every dataset it owns are deleted from the "
                "server. This cannot be undone."
            ),
        ):
            return (
                f"Not deleted. Server map `{map_id}` is still there. Ask the user to "
                f"confirm, then call again with confirm=True."
            )
        self.api.delete_map(map_id)
        return f"Deleted server map {title!r} (`{map_id}`)."

    async def _confirmed(
        self, ctx: Context | None, *, subject: str, consequence: str
    ) -> bool:
        """Put a yes/no question to the user, or report that none was asked.

        Returning False when the card could not be drawn is the whole point: a
        client that cannot elicit must not be read as a client that said yes.
        The caller's text then tells the agent to ask the user directly.
        """
        if not _can_elicit(ctx):
            return False
        try:
            result = await ctx.elicit(
                f"Delete {subject}? {consequence}", confirm_schema(subject, consequence)
            )
        except Exception:  # noqa: BLE001 - a card that cannot be drawn is a decline
            return False
        if isinstance(result, (DeclinedElicitation, CancelledElicitation)):
            return False
        return getattr(result.data, "choice", "") == "delete"


def _when(record: LocalMap) -> str:
    updated = record.updated_at()
    if updated is None:
        return ""
    return updated.strftime("%Y-%m-%d %H:%M")


def _size(count: int) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def describe_settings(settings: Settings) -> str:
    """A one-screen summary of the configuration, for diagnostics."""
    return json.dumps(
        {
            "server_url": settings.server_url,
            "maps_dir": str(settings.maps_dir),
            "config_dir": str(settings.config_dir),
            "preview_port": settings.preview_port,
            "auth0_domain": settings.auth0_domain or None,
            "login_configured": settings.login_configured,
            "viewer_version": settings.viewer_version,
            "vendor_dir": str(settings.vendor_dir),
        },
        indent=2,
    )
