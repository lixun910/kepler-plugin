"""Getting data in, and getting it back out in shapes the viewer can read.

DuckDB does the reading. It is the only dependency here that is not the standard
library or httpx, and it earns that: one call each for CSV with type inference,
Parquet, and newline-delimited JSON, over files and over `http(s)://` URLs
alike, with a schema this module can then ask about rather than guess at.

Two things this module decides, and both are decisions rather than detections
because the viewer acts on them:

**What kind of dataset it is.** `point`, `geojson`, `h3` or `table` — see
`DatasetKind` in the viewer's `types.ts`. The viewer hands `geojson` to
`processGeojson` and everything else to `processRowObject`, and those produce
different structures; a wrong answer here is a layer that renders nothing rather
than an error. So the kind is worked out once, from the data, and written into
the map's spec where the viewer reads it.

**Where the geometry is.** For a GeoJSON file it is the geometry itself. For a
table it is either a latitude/longitude *pair*, named by the two columns
`layers.py` will point the layer at, or a column of WKT. The pair's names matter
downstream: kepler's point layer is configured with `lat` and `lng` field names,
and a map whose layer names a column that does not exist renders as an empty
basemap with no complaint.

The `_geojson` column is the plugin's own convention — one GeoJSON geometry per
row, as a JSON string — and it exists because kepler's geojson layer reads a
FeatureCollection rather than a column of WKB. The viewer reassembles the
FeatureCollection from it, taking the other columns as properties.

**The spatial extension is optional and the plugin does not require it.** WKT
columns and shapefiles need DuckDB's `spatial`, which is downloaded on first use
and therefore unavailable to an offline install. Latitude/longitude CSVs,
GeoJSON and everything tabular need nothing beyond what ships in the wheel, and
those are the common cases. Where `spatial` is genuinely required and cannot be
fetched, the error says exactly that, because the alternative — an autoload
failure surfacing as a parse error — sends the reader looking at their data.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import duckdb

#: The plugin's geometry column, matching `GEOJSON_COLUMN` in the viewer.
GEOJSON_COLUMN = "_geojson"

#: Row and byte ceilings for inlining rows into a local `map.html`.
#:
#: A `file://` page cannot fetch a sibling Parquet file — Chrome treats the
#: origin as opaque and blocks it — so a local map that wants to work from
#: Finder has to carry its rows. Past these ceilings it stops being a page: the
#: rows go to `data/*.parquet` instead and the map renders over the preview
#: server, which can serve them. Both numbers are checked because they fail
#: differently: a million narrow rows makes a slow page, a few hundred
#: wide-geometry rows makes a page the browser refuses to parse.
INLINE_ROW_CAP = 200_000
INLINE_BYTE_CAP = 48 * 1024 * 1024

#: How many values to look at when deciding whether a text column holds WKT.
#: Enough to not be fooled by a single stray value, small enough to be free.
SNIFF_ROWS = 50

#: Latitude/longitude column pairs, most specific first. The pair is chosen
#: before any other column, because a file that has both `latitude`/`longitude`
#: and a generic `x`/`y` means the first pair.
LAT_CANDIDATES = ("latitude", "lat", "y", "ycoord", "y_coord", "lat_deg", "point_lat")
LNG_CANDIDATES = (
    "longitude",
    "lng",
    "lon",
    "long",
    "x",
    "xcoord",
    "x_coord",
    "lng_deg",
    "lon_deg",
    "point_lng",
)

#: Column names that look like geometry, for a WKT column.
GEOMETRY_NAME_CANDIDATES = (
    "geometry",
    "geom",
    "the_geom",
    "wkt",
    "geog",
    "shape",
    "wkb_geometry",
)

#: Column names that look like an H3 cell index.
H3_NAME_CANDIDATES = ("h3", "h3_index", "h3_id", "hex_id", "h3_cell", "h3index")


class DataError(RuntimeError):
    """The data could not be read, with a message meant for a human."""


@dataclass
class Column:
    name: str
    type: str = "VARCHAR"
    #: The key this column was built from, when the SQL name had to differ —
    #: a feature property called `_geojson` becomes the column
    #: `_geojson_property`. Insertion reads through this, so a renamed column
    #: still finds its values.
    source: str | None = None

    @property
    def is_numeric(self) -> bool:
        return any(
            self.type.upper().startswith(prefix)
            for prefix in ("INT", "BIGINT", "DOUBLE", "FLOAT", "DECIMAL", "REAL", "HUGEINT", "SMALLINT", "TINYINT", "NUMERIC")
        )

    @property
    def is_text(self) -> bool:
        return self.type.upper().startswith(("VARCHAR", "CHAR", "TEXT", "STRING"))


@dataclass
class LoadedTable:
    """One loaded dataset, described well enough to build a layer from it."""

    table: str
    source: str
    columns: list[Column]
    row_count: int
    kind: str = "table"
    #: For `kind == "point"`: the two column names the layer must be pointed at.
    lat_column: str | None = None
    lng_column: str | None = None
    #: For `kind == "geojson"`: where the geometry came from, for the report.
    geometry_note: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def describe(self) -> str:
        head = ", ".join(f"{c.name} {c.type}" for c in self.columns[:12])
        if len(self.columns) > 12:
            head += f", … {len(self.columns) - 12} more"
        return f"{self.row_count:,} rows · {head}"


def _jsonable(value: Any) -> Any:
    """Convert a DuckDB value into something `json.dumps` will accept.

    Needed because DuckDB's Python client is honest about its types: a `DATE`
    arrives as `datetime.date`, a `DECIMAL` as `decimal.Decimal`, a `BLOB` as
    `bytes`, and a `STRUCT` as a dict — and none of those serialize. The viewer
    normalises BigInt on its own side; this is the matching half on this one.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, bytes):
        # A BLOB is usually WKB, which as a string is unreadable either way;
        # hex at least round-trips and is obviously not text.
        return value.hex()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class Session:
    """An in-memory DuckDB database holding the datasets for one map."""

    def __init__(self) -> None:
        # In memory, not a file. Nothing here needs to outlive the call that
        # built the map: the output is a spec, and a spec that referenced a
        # temporary database file would be a map that stops rendering when the
        # file is cleaned up.
        self.con = duckdb.connect(database=":memory:")
        self._spatial_ready: bool | None = None
        self._counter = 0

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:  # noqa: BLE001 - closing twice must not raise
            pass

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- spatial, lazily ------------------------------------------------

    def _try_spatial(self) -> bool:
        """Load DuckDB's `spatial`, once, reporting failure as a fact not an error.

        Tried rather than assumed: `INSTALL spatial` needs network access the
        first time, and an agent running from a plugin cache may well not have
        it. The result is memoised because the load is a per-connection cost and
        the answer will not change within one session.
        """
        if self._spatial_ready is not None:
            return self._spatial_ready
        try:
            self.con.execute("INSTALL spatial")
        except Exception:  # noqa: BLE001 - already installed, or offline
            pass
        try:
            self.con.execute("LOAD spatial")
            self._spatial_ready = True
        except Exception:  # noqa: BLE001
            self._spatial_ready = False
        return self._spatial_ready

    def _require_spatial(self, why: str) -> None:
        if self._try_spatial():
            return
        raise DataError(
            f"{why} needs DuckDB's spatial extension, which is not installed and "
            f"could not be downloaded. Install it once with network access:\n\n"
            f"    python -c \"import duckdb; duckdb.connect().execute('INSTALL spatial')\"\n\n"
            f"Nothing else is affected — CSV with latitude/longitude columns, "
            f"GeoJSON and Parquet all work without it."
        )

    # -- loading ---------------------------------------------------------

    def _table_name(self, hint: str | None) -> str:
        self._counter += 1
        base = _identifier(hint or f"dataset_{self._counter}")
        return f"{base}_{self._counter}"

    def load(
        self,
        source: str,
        *,
        name: str | None = None,
        kind: str | None = None,
        lat: str | None = None,
        lng: str | None = None,
    ) -> LoadedTable:
        """Read `source` into a table and describe what came out.

        `source` is a path or an `http(s)://` URL. `kind`, `lat` and `lng` are
        overrides: they exist because detection is a guess, and the caller who
        knows the data should be able to say so rather than argue with it.
        """
        text = str(source).strip()
        if not text:
            raise DataError("No data source given.")

        table = self._table_name(name)
        if text.startswith(("http://", "https://")):
            path = self._fetch(text)
            suffix = _suffix_of(text) or path.suffix
        else:
            path = Path(text).expanduser()
            if not path.exists():
                raise DataError(
                    f"No such file: {path}. Paths are resolved relative to the "
                    f"process's working directory, which for an MCP server is not "
                    f"the directory the user is looking at — use an absolute path."
                )
            suffix = path.suffix.lower()

        loaded = self._load_path(path, table, suffix)
        loaded.kind, loaded.lat_column, loaded.lng_column, note = classify(
            self.con,
            table,
            loaded.columns,
            loaded.kind,
            override=kind,
            lat=lat,
            lng=lng,
        )
        if note:
            loaded.geometry_note = note
        if loaded.kind == "geojson":
            self._materialise_geojson(table, loaded)
        return loaded

    def _load_path(self, path: Path, table: str, suffix: str) -> LoadedTable:
        if suffix in {".csv", ".tsv", ".txt", ".psv"}:
            return self._load_csv(path, table, suffix)
        if suffix in {".parquet", ".pq"}:
            return self._load_parquet(path, table)
        if suffix in {".geojson", ".json", ".ndjson", ".jsonl"}:
            return self._load_json(path, table, suffix)
        if suffix in {".shp", ".gpkg", ".fgb", ".zip"}:
            return self._load_geo_file(path, table)
        if suffix in {".xlsx", ".xls"}:
            raise DataError(
                f"{path.name} is a spreadsheet, which this plugin does not read. "
                f"Export it as CSV from the spreadsheet's own File → Save As, then "
                f"load that."
            )
        # Last resort: let DuckDB guess from the contents. It handles a CSV named
        # `.data` and a Parquet named anything at all, which is worth one
        # attempt before refusing.
        try:
            return self._load_csv(path, table, ".csv")
        except Exception as exc:  # noqa: BLE001
            raise DataError(
                f"Do not know how to read {path.name} — unrecognised extension "
                f"{suffix or '(none)'}. Supported: CSV, TSV, Parquet, GeoJSON, JSON, "
                f"newline-delimited JSON, and shapefile/GeoPackage with the spatial "
                f"extension."
            ) from exc

    def _load_csv(self, path: Path, table: str, suffix: str) -> LoadedTable:
        options = ["header = true", "sample_size = -1"]
        if suffix in {".tsv", ".psv"}:
            options.append(f"delim = '{chr(9) if suffix == '.tsv' else '|'}'")
        try:
            # `sample_size = -1` scans every row to infer types. The default
            # samples 20k, which types a column of mostly-empty values from the
            # rows it happened to look at — and then a value that does not fit
            # arrives 400k rows later and fails the whole load.
            self.con.execute(
                f"CREATE OR REPLACE TABLE {table} AS "
                f"SELECT * FROM read_csv_auto({_literal(str(path))}, "
                f"{', '.join(options)})"
            )
        except Exception as exc:  # noqa: BLE001
            raise DataError(
                f"Could not read {path.name} as CSV — {_brief(exc)}. If the file has "
                f"no header row, or uses a delimiter other than a comma, say so: the "
                f"loader assumes a comma-separated file with a header."
            ) from exc
        return self._describe(table, str(path))

    def _load_parquet(self, path: Path, table: str) -> LoadedTable:
        try:
            self.con.execute(
                f"CREATE OR REPLACE TABLE {table} AS "
                f"SELECT * FROM read_parquet({_literal(str(path))})"
            )
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"Could not read {path.name} as Parquet — {_brief(exc)}") from exc
        return self._describe(table, str(path))

    def _load_json(self, path: Path, table: str, suffix: str) -> LoadedTable:
        """GeoJSON is parsed in Python; plain JSON goes through DuckDB.

        The split is not arbitrary. A FeatureCollection's geometry is a nested
        object that `read_json_auto` flattens into columns named
        `geometry.coordinates[1]` and the like, which is worse than useless —
        the coordinates are there and unusable. Reading the file as text and
        walking it costs a parse and produces exactly the rows the viewer wants.
        """
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise DataError(f"Could not read {path.name} — {exc}") from exc

        if not raw.strip():
            raise DataError(f"{path.name} is empty.")
        if suffix == ".ndjson" or suffix == ".jsonl":
            return self._load_ndjson(path, table)

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DataError(
                f"{path.name} is not valid JSON — {exc}. A file that is one JSON "
                f"object per line should be named .ndjson or .jsonl."
            ) from exc

        if _looks_like_geojson(parsed):
            return self._load_geojson_document(path, table, parsed)
        if isinstance(parsed, list):
            return self._load_object_array(path, table, parsed)
        if isinstance(parsed, dict):
            # A single record, or an envelope like `{"data": [...]}`. The first
            # list of dicts found at the top level is taken as the rows, which
            # covers every wrapper shape seen in practice without a schema.
            for key, value in parsed.items():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    loaded = self._load_object_array(path, table, value)
                    loaded.warnings.append(f"read rows from the {key!r} key")
                    return loaded
            return self._load_object_array(path, table, [parsed])
        raise DataError(
            f"{path.name} holds a JSON {type(parsed).__name__}, which is not a set of "
            f"rows. Expected an array of objects or a GeoJSON FeatureCollection."
        )

    def _load_object_array(self, path: Path, table: str, rows: list) -> LoadedTable:
        """Build a table from a list of JSON objects, unioning their keys.

        DuckDB's `read_json_auto` would also do this, but it samples for its
        schema and a property that appears only in the last record of a file
        then vanishes. Reading every object's keys is O(rows) over data already
        in memory, and it cannot lose a column.
        """
        for row in rows:
            if not isinstance(row, dict):
                raise DataError(
                    f"{path.name} holds a JSON array whose entries are not all "
                    f"objects, so it is not a set of rows."
                )
        self.create_from_objects(table, rows)
        return self._describe(table, str(path))

    def from_rows(self, table: str, rows: list[dict[str, Any]]) -> LoadedTable:
        """Build a typed table from rows already in memory.

        Used to round-trip a local map's inline rows back into Parquet for
        upload, so what the server stores is what the local page renders.
        """
        if not rows:
            raise DataError("No rows to load.")
        self.create_from_objects(table, rows)
        return self._describe(table, table)

    def create_from_objects(self, table: str, rows: list[dict[str, Any]]) -> None:
        """Create `table` from a list of dicts, with types inferred per column.

        The types matter and are not cosmetic. A JSON file of
        `[{"lat": 1.5, "lng": 2.5}]` loaded as VARCHAR is a table whose
        coordinates are strings, and `classify` refuses to call a non-numeric
        column a coordinate — so the map opens with no layer, having done
        nothing wrong at any point. Inferring the type here is what keeps
        `kind='point'` reachable for a JSON source.

        A column whose values do not agree is VARCHAR, and each value is
        serialised into it. Nested objects and arrays land there too: a single
        column cannot hold a list, and a string is more useful than dropping it.
        """
        keys: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        if not keys:
            raise DataError("No columns found in the given rows.")

        columns = infer_columns(rows, keys)
        columns_sql = ", ".join(
            f"{_quote_ident(c.name)} {c.type}" for c in columns
        )
        self.con.execute(f"CREATE OR REPLACE TABLE {table} ({columns_sql})")

        placeholders = ", ".join("?" for _ in keys)
        insert = f"INSERT INTO {table} VALUES ({placeholders})"
        batch = []
        for row in rows:
            batch.append(
                tuple(
                    _value_for_type(
                        row.get(column.source or column.name), column.type
                    )
                    for column in columns
                )
            )
            if len(batch) >= 10_000:
                self.con.executemany(insert, batch)
                batch.clear()
        if batch:
            self.con.executemany(insert, batch)

    def _load_ndjson(self, path: Path, table: str) -> LoadedTable:
        try:
            self.con.execute(
                f"CREATE OR REPLACE TABLE {table} AS "
                f"SELECT * FROM read_json_auto({_literal(str(path))}, "
                f"format = 'newline_delimited')"
            )
        except Exception as exc:  # noqa: BLE001
            raise DataError(
                f"Could not read {path.name} as newline-delimited JSON — {_brief(exc)}"
            ) from exc
        return self._describe(table, str(path))

    def _load_geojson_document(self, path: Path, table: str, parsed: Any) -> LoadedTable:
        """Turn a GeoJSON document into a table with a `_geojson` column."""
        if isinstance(parsed, dict) and parsed.get("type") == "Feature":
            features = [parsed]
        elif isinstance(parsed, dict) and parsed.get("type") == "FeatureCollection":
            features = parsed.get("features") or []
        elif isinstance(parsed, dict) and parsed.get("type") in _GEOMETRY_TYPES:
            features = [{"type": "Feature", "geometry": parsed, "properties": {}}]
        else:
            raise DataError(
                f"{path.name} is JSON but not GeoJSON: no `type` of Feature, "
                f"FeatureCollection or a geometry type."
            )
        if not features:
            raise DataError(f"{path.name} holds a FeatureCollection with no features.")

        keys: list[str] = []
        seen: set[str] = set()
        for feature in features:
            for key in (feature.get("properties") or {}):
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        properties = [feature.get("properties") or {} for feature in features]

        # Property columns are typed from the values, for the same reason the
        # JSON loader types them: a size field on a VARCHAR column cannot be
        # ramped, and a numeric property that arrives as text silently produces
        # a layer with a single dot size.
        columns = [Column(name=GEOJSON_COLUMN, type="VARCHAR")]
        columns.extend(infer_columns(properties, keys, prefix=GEOJSON_COLUMN))

        columns_sql = ", ".join(f"{_quote_ident(c.name)} {c.type}" for c in columns)
        self.con.execute(f"CREATE OR REPLACE TABLE {table} ({columns_sql})")

        # Null geometry is real — a FeatureCollection may carry features with
        # `geometry: null` — and the viewer skips those rows rather than
        # failing, so they are stored as NULL and counted.
        null_geometries = 0
        insert = f"INSERT INTO {table} VALUES ({', '.join('?' for _ in columns)})"
        batch = []
        for feature in features:
            geometry = feature.get("geometry")
            if geometry is None:
                null_geometries += 1
                encoded = None
            else:
                encoded = json.dumps(geometry, separators=(",", ":"))
            row = feature.get("properties") or {}
            batch.append(
                (encoded,)
                + tuple(
                    _value_for_type(row.get(c.source or c.name), c.type)
                    for c in columns[1:]
                )
            )
            if len(batch) >= 10_000:
                self.con.executemany(insert, batch)
                batch.clear()
        if batch:
            self.con.executemany(insert, batch)

        loaded = self._describe(table, str(path))
        loaded.kind = "geojson"
        loaded.geometry_note = f"{len(features):,} features from the file"
        if null_geometries:
            loaded.warnings.append(
                f"{null_geometries:,} feature(s) have no geometry and will not be drawn"
            )
        return loaded

    def _load_geo_file(self, path: Path, table: str) -> LoadedTable:
        """Shapefile, GeoPackage and friends, via DuckDB's spatial `ST_Read`."""
        self._require_spatial(f"Reading {path.suffix} files")
        try:
            self.con.execute(
                f"CREATE OR REPLACE TABLE {table} AS "
                f"SELECT * FROM ST_Read({_literal(str(path))})"
            )
        except Exception as exc:  # noqa: BLE001
            raise DataError(
                f"Could not read {path.name} — {_brief(exc)}. A shapefile needs its "
                f"companion files (.shp, .shx, .dbf, .prj) together in one directory; "
                f"a .zip holding them all is also accepted."
            ) from exc
        loaded = self._describe(table, str(path))
        loaded.geometry_note = "read with the spatial extension"
        return loaded

    def _fetch(self, url: str) -> Path:
        """Download a URL to a temporary file.

        Downloaded rather than read over DuckDB's `httpfs`, for two reasons: the
        result can be sniffed by extension and by content, which a remote
        `read_csv_auto` cannot do before committing to a parser; and the byte
        count is known, which is what the hosted half of the plugin bills
        against egress.
        """
        import httpx

        suffix = _suffix_of(url)
        try:
            with httpx.Client(timeout=120.0, follow_redirects=True) as client:
                response = client.get(url)
                response.raise_for_status()
                payload = response.content
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"Could not download {url} — {_brief(exc)}") from exc

        handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        handle.write(payload)
        handle.close()
        return Path(handle.name)

    # -- describing ------------------------------------------------------

    def _describe(self, table: str, source: str) -> LoadedTable:
        rows = self.con.execute(f"DESCRIBE {table}").fetchall()
        columns = [Column(name=str(r[0]), type=str(r[1])) for r in rows]
        count = self.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        return LoadedTable(table=table, source=source, columns=columns, row_count=int(count))

    def _materialise_geojson(self, table: str, loaded: LoadedTable) -> None:
        """Fill in `_geojson` for a table whose geometry is not a column yet.

        Called after classification, because whether it is needed depends on the
        verdict: a table with a WKT column is only turned into GeoJSON if it was
        decided to be a `geojson` dataset, and a table with latitude/longitude
        is not turned into one at all — kepler's point layer is better than a
        point FeatureCollection, and it keeps the properties in the row.
        """
        if GEOJSON_COLUMN in loaded.column_names:
            return
        wkt_column = _find_wkt_column(self.con, table, loaded.columns)
        if not wkt_column:
            return
        self._require_spatial(f"Converting the {wkt_column!r} column to GeoJSON")
        try:
            self.con.execute(
                f"ALTER TABLE {table} ADD COLUMN {GEOJSON_COLUMN} VARCHAR"
            )
            self.con.execute(
                f"UPDATE {table} SET {GEOJSON_COLUMN} = "
                f"ST_AsGeoJSON(ST_GeomFromText({_quote_ident(wkt_column)}))"
            )
        except Exception as exc:  # noqa: BLE001
            raise DataError(
                f"Could not convert the {wkt_column!r} column to GeoJSON — "
                f"{_brief(exc)}. Values that are not valid WKT, or that use a "
                f"geometry type the conversion does not handle, will do this."
            ) from exc
        loaded.columns.append(Column(name=GEOJSON_COLUMN, type="VARCHAR"))
        loaded.geometry_note = f"converted from the {wkt_column!r} WKT column"

    # -- reading back out -------------------------------------------------

    def rows(self, table: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Rows as JSON-safe dicts, for inlining into a page."""
        sql = f"SELECT * FROM {table}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        cursor = self.con.execute(sql)
        names = [d[0] for d in cursor.description]
        return [
            {name: _jsonable(value) for name, value in zip(names, row)}
            for row in cursor.fetchall()
        ]

    def inline_size(self, table: str) -> int:
        """The approximate JSON size of a table's rows, without building them.

        DuckDB can measure this itself with `to_json`, which is far cheaper than
        materialising the rows in Python only to measure them — and this runs
        before the decision to inline, so it should not itself cost what it is
        trying to avoid.
        """
        try:
            total = self.con.execute(
                f"SELECT sum(length(to_json(t))) FROM {table} t"
            ).fetchone()[0]
            return int(total or 0)
        except Exception:  # noqa: BLE001 - a measurement failure is not fatal
            # Fall back to a column count times a row guess; being wrong here
            # only moves the inline/parquet boundary.
            return 0

    def to_parquet(self, table: str) -> bytes:
        """The table as Parquet bytes.

        Written through a real temporary file rather than to a buffer, because
        DuckDB's `COPY … TO` takes a path. The file is removed in the `finally`,
        including on failure — a leaked temp file per failed map would be a slow
        way to fill a disk.
        """
        handle = tempfile.NamedTemporaryFile(suffix=".parquet", delete=False)
        handle.close()
        path = Path(handle.name)
        try:
            self.con.execute(
                f"COPY {table} TO {_literal(str(path))} (FORMAT parquet, COMPRESSION zstd)"
            )
            return path.read_bytes()
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"Could not write Parquet — {_brief(exc)}") from exc
        finally:
            path.unlink(missing_ok=True)

    def sample(self, table: str, limit: int = 5) -> list[dict[str, Any]]:
        return self.rows(table, limit=limit)

    def count(self, table: str) -> int:
        """How many rows a loaded table has."""
        return int(self.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

    def count_parquet(self, path: Path) -> int:
        """How many rows a Parquet file on disk has.

        Read from the file's footer rather than by scanning it, which is the
        point: this is called on the upload path to tell the server what it is
        storing, and a count that read every row would double the work of an
        upload to produce a number Parquet already knows.
        """
        try:
            return int(
                self.con.execute(
                    "SELECT count(*) FROM read_parquet(?)", [str(path)]
                ).fetchone()[0]
            )
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"Could not count rows in {path.name} — {_brief(exc)}") from exc


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(
    con: duckdb.DuckDBPyConnection,
    table: str,
    columns: list[Column],
    current: str,
    *,
    override: str | None = None,
    lat: str | None = None,
    lng: str | None = None,
) -> tuple[str, str | None, str | None, str | None]:
    """Decide the dataset's kind, and which columns carry its geometry.

    Returns `(kind, lat_column, lng_column, note)`.

    The order is deliberate: an explicit override wins over everything, then a
    geometry already established by the loader (a GeoJSON file), then a
    latitude/longitude pair, then WKT, then H3, then a plain table. Latitude and
    longitude before WKT because a file that has both is a file whose author
    wanted points, and a point layer over the same table is faster to draw and
    keeps every property.
    """
    if override:
        kind = override.lower().strip()
        if kind not in {"point", "geojson", "h3", "table"}:
            raise DataError(
                f"Unknown kind {override!r}. Use one of: point, geojson, h3, table."
            )
        if kind == "point":
            found_lat, found_lng = _find_lat_lng(columns, lat, lng)
            if not found_lat or not found_lng:
                raise DataError(
                    f"kind='point' needs latitude and longitude columns. The table "
                    f"has: {', '.join(c.name for c in columns)}. Pass lat= and lng= "
                    f"to name them."
                )
            return kind, found_lat, found_lng, None
        return kind, None, None, None

    if current == "geojson":
        return "geojson", None, None, None

    # A geometry column that DuckDB's spatial reader produced — a shapefile or
    # GeoPackage read through ST_Read.
    geometry_column = next(
        (c for c in columns if c.type.upper().startswith("GEOMETRY")), None
    )
    if geometry_column is not None:
        return "geojson", None, None, "geometry column from the file"

    found_lat, found_lng = _find_lat_lng(columns, lat, lng)
    if found_lat and found_lng:
        return "point", found_lat, found_lng, f"latitude/longitude in {found_lat}, {found_lng}"

    if _find_wkt_column(con, table, columns):
        return "geojson", None, None, None

    h3_column = _find_h3_column(con, table, columns)
    if h3_column:
        return "h3", None, None, f"H3 cell ids in {h3_column}"

    return "table", None, None, None


def _find_lat_lng(
    columns: list[Column], lat: str | None, lng: str | None
) -> tuple[str | None, str | None]:
    """Pick the latitude and longitude columns, or the pair the caller named.

    A named pair is used as given and *not* checked against the column list — the
    check happens when the query runs, and failing there reports the actual
    problem ("column X does not exist") rather than a wrong guess about intent.
    """
    if lat or lng:
        return lat, lng

    by_name = {c.name.lower(): c for c in columns}
    chosen_lat = next(
        (by_name[name] for name in LAT_CANDIDATES if name in by_name), None
    )
    chosen_lng = next(
        (by_name[name] for name in LNG_CANDIDATES if name in by_name), None
    )
    if chosen_lat is None or chosen_lng is None:
        return None, None

    # Both have to be numeric. A file with a column literally named `lat` that
    # holds place *names* is not a file of coordinates, and treating it as one
    # produces a map of the null island or nothing at all.
    if not (chosen_lat.is_numeric and chosen_lng.is_numeric):
        return None, None
    return chosen_lat.name, chosen_lng.name


def _find_wkt_column(
    con: duckdb.DuckDBPyConnection, table: str, columns: list[Column]
) -> str | None:
    """A text column holding WKT, by name first and then by inspection."""
    text_columns = [c for c in columns if c.is_text]
    by_name = {c.name.lower(): c.name for c in text_columns}

    named = next(
        (by_name[name] for name in GEOMETRY_NAME_CANDIDATES if name in by_name), None
    )
    if named and _values_look_like_wkt(con, table, named):
        return named

    # No column named like geometry. Looking at every text column would be a
    # scan per column on a wide table, so only the first few are tried — a WKT
    # column in a wide file is nearly always near the front, and the cost of
    # missing one is a table view rather than a broken map.
    for column in text_columns[:6]:
        if _values_look_like_wkt(con, table, column.name):
            return column.name
    return None


def _values_look_like_wkt(
    con: duckdb.DuckDBPyConnection, table: str, column: str
) -> bool:
    try:
        rows = con.execute(
            f"SELECT {_quote_ident(column)} FROM {table} "
            f"WHERE {_quote_ident(column)} IS NOT NULL LIMIT {SNIFF_ROWS}"
        ).fetchall()
    except Exception:  # noqa: BLE001
        return False
    values = [str(r[0]).strip().upper() for r in rows if r[0] is not None]
    if not values:
        return False
    # Every non-null value must look like WKT. Most is not enough: a free-text
    # column that mentions "POINT" once would otherwise be converted, and the
    # conversion would fail on the rest of the table.
    return all(
        value.startswith(_WKT_PREFIXES) and "(" in value for value in values
    )


_WKT_PREFIXES = (
    "POINT",
    "LINESTRING",
    "POLYGON",
    "MULTIPOINT",
    "MULTILINESTRING",
    "MULTIPOLYGON",
    "GEOMETRYCOLLECTION",
)

_GEOMETRY_TYPES = (
    "Point",
    "LineString",
    "Polygon",
    "MultiPoint",
    "MultiLineString",
    "MultiPolygon",
    "GeometryCollection",
)


def _find_h3_column(
    con: duckdb.DuckDBPyConnection, table: str, columns: list[Column]
) -> str | None:
    """A column of H3 cell ids: 15 or 16 hex characters, named like one.

    Both conditions are required. The name alone would match a column of
    arbitrary hex — an md5, a colour — and the shape alone would match any
    hex column in any table, which is a lot of tables.
    """
    by_name = {c.name.lower(): c.name for c in columns if c.is_text}
    for candidate in H3_NAME_CANDIDATES:
        if candidate not in by_name:
            continue
        column = by_name[candidate]
        try:
            rows = con.execute(
                f"SELECT {_quote_ident(column)} FROM {table} "
                f"WHERE {_quote_ident(column)} IS NOT NULL LIMIT {SNIFF_ROWS}"
            ).fetchall()
        except Exception:  # noqa: BLE001
            continue
        values = [str(r[0]).strip() for r in rows if r[0] is not None]
        if values and all(
            len(v) in {15, 16} and all(ch in "0123456789abcdefABCDEF" for ch in v)
            for v in values
        ):
            return column
    return None


def _looks_like_geojson(parsed: Any) -> bool:
    if not isinstance(parsed, dict):
        return False
    kind = parsed.get("type")
    return kind in {"FeatureCollection", "Feature", *_GEOMETRY_TYPES}


def infer_columns(
    rows: list[dict[str, Any]],
    keys: list[str],
    *,
    prefix: str | None = None,
) -> list[Column]:
    """A DuckDB type for each key, from the values actually present.

    The rule is that a column is numeric only if *every* non-null value is a
    number. A single `"N/A"` in a column of counts makes the whole column text,
    which is the honest answer: casting it would turn that value into NULL and
    the map would report a count the file does not contain.

    `prefix` names a column that is already in the table, so a property called
    `_geojson` does not collide with the geometry column.
    """
    columns: list[Column] = []
    for key in keys:
        if key == prefix:
            # A feature property literally named `_geojson` would otherwise
            # produce a second column of that name and the CREATE TABLE fails.
            columns.append(
                Column(name=f"{key}_property", type="VARCHAR", source=key)
            )
            continue
        seen: list[Any] = [row.get(key) for row in rows if row.get(key) is not None]
        if not seen:
            columns.append(Column(name=key, type="VARCHAR", source=key))
            continue
        if all(isinstance(v, bool) for v in seen):
            sql_type = "BOOLEAN"
        elif all(isinstance(v, int) and not isinstance(v, bool) for v in seen):
            sql_type = "BIGINT"
        elif all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in seen
        ):
            sql_type = "DOUBLE"
        else:
            sql_type = "VARCHAR"
        columns.append(Column(name=key, type=sql_type, source=key))
    return columns


def _value_for_type(value: Any, sql_type: str) -> Any:
    """Coerce a Python value to what its column's type will accept.

    The mismatch is real and not rare: a JSON column that is numeric except for
    one blank string is typed VARCHAR, and the numbers then have to be
    stringified to insert. DuckDB will not do it implicitly, and the failure is
    a cast error naming a row number.
    """
    if value is None:
        return None
    kind = sql_type.upper()
    if kind == "BOOLEAN":
        return bool(value)
    if kind in {"BIGINT", "INTEGER", "SMALLINT", "TINYINT"}:
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    if kind in {"DOUBLE", "REAL", "FLOAT"}:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return _scalar_for_column(value)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _identifier(text: str) -> str:
    """A SQL-safe table name from a label."""
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in text).strip("_")
    cleaned = cleaned or "dataset"
    if cleaned[0].isdigit():
        cleaned = f"t_{cleaned}"
    return cleaned[:48].lower()


def _quote_ident(name: str) -> str:
    """Quote a SQL identifier, doubling any embedded quote.

    Column names come from the user's file, so they can contain anything —
    including a double quote, which is how a column named `a" ; DROP TABLE x--`
    would otherwise become SQL. Doubling is the escape DuckDB uses.
    """
    return '"' + str(name).replace('"', '""') + '"'


def _literal(text: str) -> str:
    """A SQL string literal, with quotes escaped."""
    return "'" + str(text).replace("'", "''") + "'"


def _brief(exc: BaseException) -> str:
    """The first line of an exception message.

    DuckDB's errors carry a multi-line payload with the query, a caret and a
    line number. All of it is useful in a terminal and none of it is useful
    inside a sentence in a chat window.
    """
    message = str(exc).strip()
    first = message.splitlines()[0] if message else exc.__class__.__name__
    return first[:300]


def _suffix_of(url: str) -> str:
    from urllib.parse import urlparse

    return Path(urlparse(url).path).suffix.lower()


def _scalar_for_column(value: Any) -> Any:
    """Flatten a JSON value for a VARCHAR column.

    Nested objects and arrays are re-serialised rather than dropped: a property
    that is a list of tags is still worth having on the map, and a string is the
    only thing a single column can hold.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, separators=(",", ":"))


def summarise(rows: Iterable[dict], limit: int = 3) -> str:
    """A short markdown table of sample rows, for tool output."""
    rows = list(rows)[:limit]
    if not rows:
        return "_(no rows)_"
    headers = list(rows[0].keys())[:8]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        cells = []
        for header in headers:
            value = row.get(header)
            text = "" if value is None else str(value)
            if len(text) > 40:
                text = text[:37] + "…"
            cells.append(text.replace("|", "\\|"))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
