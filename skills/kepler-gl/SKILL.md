---
name: kepler-gl
description: Build kepler.gl maps from a CSV, Parquet file, GeoJSON file, shapefile, JSON or newline-delimited JSON — local pages the user can open, edit and save — and, when signed in, upload them to a kepler.gl server that stores the map config and the data as Parquet. Use when the user wants to map data, plot locations, draw polygons, make a choropleth or heatmap, list the maps they have made, or share a map with someone.
---

# Building kepler.gl maps

Fifteen tools, split into two halves that do not depend on each other.

**The local half needs no account and no network.** It reads a file, decides
what the data is, writes a kepler.gl map as a directory holding a page, and
serves that page on a loopback port so it can be edited. Everything from a CSV
to a finished map happens offline.

**The hosted half needs a sign-in.** It puts a local map on the kepler.gl
server, where projects group maps and the config and data are stored as Parquet.
Nothing here is required to make a map.

| Local | Hosted |
| --- | --- |
| `inspect_data`, `create_map`, `list_maps`, `open_map`, `delete_map` | `list_projects`, `create_project`, `list_server_maps`, `upload_map`, `update_server_map`, `open_server_map`, `delete_server_map` |

Plus `auth_status`, `login`, `logout`.

## The workflow

1. **`inspect_data`** on anything unfamiliar. It loads the file, classifies it,
   and reports the columns with their types, which columns were taken as the
   geometry, which layer type that implies, and a few sample rows. It writes
   nothing. Every argument `create_map` takes can be rehearsed here for free —
   do this before a map the data might not suit, because a layer built on the
   wrong column fails silently and reads as an empty basemap.
2. **`create_map`** with the same sources. It returns a **URL** and a path.
3. **Give the user the URL, not the path.** A `file://` page cannot POST, so it
   cannot save its edits, and Chrome blocks it from fetching a sibling Parquet
   file — so a map whose data is too big to inline cannot even render from it.
   The path is worth naming too: it is what the user keeps and sends to someone
   else.
4. **`list_maps`** then **`open_map`** to come back to a map. `open_map` takes
   the directory name, re-renders the page against the current viewer, and
   returns a fresh URL. `list_maps` also returns the URL of the **map index**:
   every local map on one page, a card each with a drawing of its data. That URL
   is what to give a user who wants to look at what they have made rather than
   read a table of it — see below.
5. **Sign in only to upload.** `auth_status` says whether an account is
   connected; `login` opens a browser. Then `list_projects` / `create_project`,
   and `upload_map` with a local map's directory name.

## The map index

`list_maps` returns a table for an agent to read *and* the URL of an index page
for a person to look at. It is served from the same loopback port as the maps,
at the server's root, and it needs no account.

Each card carries the title, the description, the datasets and layers, when the
map was last edited, and a thumbnail. **The thumbnail is drawn from the map's own
spec** — the coordinates inlined in it, in the colours the map draws them in: the
layer's colour, or, where the layer maps a column to colour, the colour that
value falls in on the layer's colour range. It is not a live frame of the map:
twenty iframes would each want a WebGL context, and browsers run out at around
sixteen. A map whose data is held in `data/*.parquet`, or an
H3 or `table` dataset, has no coordinates in the spec to draw from and gets a
labelled placeholder instead, saying which it is. That is not an error; it is the
one case the thumbnail cannot cover.

The page is rendered from a fresh scan each time it is loaded, so a map deleted
from Finder or copied in from a colleague is right there on the next reload.
Nothing has to be refreshed by hand.

**Give the user this URL when they ask what maps they have.** The reply to
`list_maps` carries it; `create_map` and `open_map` name it too. It is one link
that answers "what have I made" for every map at once.

## What decides whether a map draws anything

`create_map` builds every layer itself, from what was actually loaded. kepler's
own auto-creation is off whenever a config is present, and for a GeoJSON or H3
dataset it would produce no layer at all — so nothing here is left to a guess.

**The file's columns are what decide the layer**, not its extension:

| What was found | `kind` | Layer built |
| --- | --- | --- |
| A numeric latitude/longitude pair | `point` | `point` |
| A GeoJSON geometry column (`_geojson`) | `geojson` | `geojson` |
| A column of H3 cell ids | `h3` | `hexagonId` |
| Neither | `table` | none — the columns still load and appear in the tooltip |

A `table` dataset is not a failure. The map opens, the panel lists the dataset
with all its columns, and the user picks a layer type. But if the file *does*
have coordinates the plugin missed, say so and pass them explicitly:
`options={"*": {"lat": "Lat", "lng": "Lon"}}`.

`inspect_data` is the reliable way to learn which branch a file takes. When it
reports `kind **table**` and the user expected points, that is the moment to
name the columns — not after the map is open and blank.

### Coordinate columns are found by name and by value

`latitude`/`lat`/`y` and `longitude`/`lon`/`lng`/`x`, case-insensitively, and
only when the column is numeric — a column of coordinate *strings* is not
adopted, because a point layer on a VARCHAR column draws nothing. A WKT column
is converted through DuckDB's spatial extension, which is downloaded on first
use and therefore absent on a machine with no network; when that happens the
report says so rather than failing obscurely.

## Choosing a layer type

Override the default with `layer_type` (applies to every dataset) or
`options[source]["layer_type"]` (one dataset). The types and what each needs:

`point`, `heatmap`, `grid`, `hexbin`, `cluster` — latitude and longitude.
`geojson`, `3d` — a geometry column; `3d` extrudes polygons by a height field.
`hexagonId` — an H3 cell id column.
`arc`, `line` — four columns, named through `source_lat`, `source_lng`,
`target_lat`, `target_lng`.

Per-layer config detail — which fields exist, what the defaults are, what a
choropleth needs — is in [references/layer-types.md](references/layer-types.md).

### Colouring by a value

`color_field` and `size_field` name a column and are the difference between a
map that shows a pattern and a map that shows a location. They are checked
against the dataset's real columns, and a name that is not there raises an error
naming the ones that are — kepler itself accepts an unknown field silently and
then draws the layer without the ramp, which looks like the colouring simply
did not apply.

When the user asks for colour or size and does not say what to use, pick the
column the question is about — magnitude, rate, population, count — and say
which one you picked. `color_scale` defaults to `quantile` (good for skewed
data) and `size_scale` to `linear`.

## Big data, and the one thing that changes

A dataset is inlined into the page below **200,000 rows and 48 MB**. Past either
limit it is written to `data/*.parquet` beside the map and loaded over http:

- the map still works, but **only from the preview URL** — from `file://` the
  browser blocks the fetch and the dataset is dropped with a note on the page;
- Parquet is columnar and compressed, so a file that is too big to inline as
  JSON is often far smaller on disk and reads faster.

Nothing about this is silent: `create_map` reports which datasets took which
path, and the page itself says so when one was dropped.

## Rules worth stating before a map is made

- **A map with no layer is a basemap.** If `create_map` reports "no layers", the
  data had no geometry the plugin could find. Fix that before handing the URL
  over.
- **Do not open a map from the filesystem.** The path works for a small inlined
  map and fails for a Parquet-backed one, and it never saves. Use the URL.
- **`delete_map` and `delete_server_map` ask first.** They are destructive and
  nothing here undoes them. Do not pass `confirm=True` unless the user has
  agreed to that specific map going.
- **Uploading is a copy, not a move.** The local map is left exactly as it was.
- **A server map can be edited at its own URL** with the same viewer, and each
  Save writes the config back to the server. `update_server_map` is for pushing
  a local map over an existing server map — its id, project and URL are kept.
- **The datasets uploaded are what the map renders**, not the files it was built
  from. A source CSV edited since the map was made does not change what goes up.

## Hosted storage and billing

The server keeps one free map and one free dataset per account; past that it is
pay-as-you-go, and what is metered is the storage the Parquet occupies and the
bytes served back through the app's download route. A map opened many times
costs more than a map opened once, which is worth saying when a user is deciding
whether to upload a large dataset rather than keep it local.

## References

- [Layer types](references/layer-types.md) — per-layer config, the fields that
  matter, and what a choropleth or an arc layer actually needs.
- [Troubleshooting](references/troubleshooting.md) — the failure modes that do
  not announce themselves: a layer that draws nothing, a map that will not save,
  a dataset that disappears under `file://`.
