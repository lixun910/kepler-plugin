# Troubleshooting

The failures worth knowing, in the order they are likely to be met. Most of
them share a property: kepler.gl does not report them. A layer whose `columns`
name a column that is not in the dataset draws nothing, logs nothing, and leaves
the user looking at a basemap.

## The map opens and draws nothing

The commonest failure, and it has three causes.

1. **No layer was built.** `create_map` says so — `Layers: no layers`, or a note
   that a dataset "has no geometry". The data classified as `kind table`: no
   numeric coordinate pair, no geometry column, no H3 ids. Run `inspect_data`
   and look at the columns it reports, then either name the coordinates
   (`options={"*": {"lat": "Latitude", "lng": "Longitude"}}`), say `kind` is
   `geojson` if a geometry column was not recognised, or accept that the dataset
   has no geometry to draw.
2. **The layer names a column that is not there.** This happens when a config
   was hand-edited or reused across datasets. The plugin checks every field it
   writes and raises with the real column list; kepler does not, so a config
   edited outside the plugin fails quietly. Compare the layer's `columns.lat`
   and `columns.lng` against `spec.json`'s datasets.
3. **The data did not load.** A Parquet-backed map opened from `file://` drops
   those datasets — Chrome blocks a file page from fetching a sibling file. The
   page carries a note saying so, in the header's subtitle. Open the preview URL
   instead.

## "Save failed" on a local map

- **The map was opened from the filesystem.** A `file://` page cannot POST, so
  the plugin's HTML deletes the save target before the viewer loads and the page
  shows "Local map — not connected to a server" rather than a button that always
  fails. If a Save button *is* showing and failing, the page is being served —
  reload it from the URL `open_map` returns.
- **"This save endpoint is not open to this page."** The save token in the page
  no longer matches the running server — the plugin process restarted, or the
  page is a stale tab. `open_map` again and reload.
- **The preview port is taken.** The preview server binds an ephemeral port by
  default, so this is rare; `KEPLER_GL_PREVIEW_PORT` pins one. The error names
  the address it could not bind.

## The data is a table of strings

Coordinates that arrived as text — `"37.7749"` rather than `37.7749` — are not
adopted as coordinates, because a point layer on a VARCHAR column draws nothing.
`inspect_data` shows the column types; a `VARCHAR` column that should be
`DOUBLE` is the signal. Fix it at the source, or cast it in a query and export
that.

The same applies to a size or colour field: a numeric ramp on a text column
produces a single size for every point.

## WKT, shapefiles and GeoPackages

These go through DuckDB's `spatial` extension, which is **downloaded on first
use**. On a machine with no network the extension is absent and the load fails
with a message saying so — it is not a corrupt file. Everything else (CSV,
Parquet, GeoJSON, JSON, NDJSON) needs no extension and works offline.

A shapefile needs its `.shp`, `.shx`, `.dbf` and `.prj` together in one
directory; a `.zip` holding all of them is also accepted.

## The map opens somewhere the data is not

`create_map` fits the view to the data, and stops doing so once a viewport has
been saved — otherwise reopening a map would silently discard the zoom the user
chose. To place the view by hand, pass `latitude`, `longitude` and/or `zoom`;
supplying any of the three turns the automatic fit off for that map.

One case looks like a bug and is not: a dataset whose extent crosses the
antimeridian — every US county, for instance, because of the Aleutian islands —
has bounds that span the globe, and kepler fits the whole world. Zoom in, or
pass an explicit viewport.

## Uploading fails

- **"Uploading needs a kepler.gl account."** No Auth0 application is configured
  on this install, so there is nothing to sign in to. `KEPLER_GL_AUTH0_DOMAIN`,
  `KEPLER_GL_AUTH0_CLIENT_ID` and `KEPLER_GL_AUTH0_AUDIENCE` have to be set.
  Local maps are unaffected.
- **A 402 from the server** is the billing gate: the account has used its free
  map and dataset and has no payment method. The error text says which.
- **A 401** means the token expired or was revoked. `login` again; the provider
  refreshes automatically on the next call, so a 401 that survives a retry is a
  real sign-out.
- **"The bytes never reached storage."** The upload is three calls — the server
  reserves a row and hands back a URL, the bytes go straight to storage, and a
  commit confirms they arrived. This message is the commit finding nothing
  there, and the plugin already sends the bytes a second time before reporting
  it. A dataset that fails twice is a connection problem rather than a map
  problem, and the row the server reserved is deleted the next day if the bytes
  never come.
- **"Storage refused the upload."** The presigned URL is valid for an hour; a
  stalled upload can outlive it. Calling the tool again mints a fresh one. A
  403 on the very first attempt instead means the bucket's credentials are
  wrong, which is the server's configuration and not something to retry.
- **A very large dataset times out.** The upload has a ten-minute ceiling. A
  dataset that big is worth keeping local — it costs nothing there, and on the
  server both the storage and every read of it are metered.

## Checking the configuration

`auth_status` reports the auth mode (anonymous, OAuth, pinned development
token), the server URL, where the token cache lives, and how many local maps
there are. It makes no network request and never opens a browser, so it is safe
to call first when something hosted is failing.

`inspect_data` is the equivalent for data: it touches nothing and answers what a
file actually contains.
