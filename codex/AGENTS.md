# Maps with kepler.gl

You can turn a data file into an interactive kepler.gl map — a page the user can
open, edit and save — through the `kepler-gl` MCP server. Its tools are
`auth_status`, `login`, `logout`, `inspect_data`, `create_map`, `list_maps`,
`open_map`, `delete_map`, `list_projects`, `create_project`, `list_server_maps`,
`upload_map`, `update_server_map`, `delete_server_map` and `open_server_map`.

**Before making a map, read the full reference at
`<PLUGIN_ROOT>/skills/kepler-gl/SKILL.md`.** Replace `<PLUGIN_ROOT>` with the
absolute path to the plugin checkout — the same substitution the `config.toml`
block needs. It carries the layer-by-layer config detail, what each layer type
needs in the data, and the failure modes that do not announce themselves.

If the plugin is installed (`scripts/install_codex_plugin.sh`), that reference
ships inside it as the `kepler-gl` skill and loads on its own — this file is then
only needed for a session where the plugin is not enabled.

## Working order

1. **Look at the data before you map it.** `inspect_data` on the source — a path
   or an http(s) URL. It writes nothing, and returns the columns with their
   types, which of them were taken as the geometry, which layer that implies,
   and a few sample rows. The failure it prevents is silent: a layer built on
   the wrong column draws nothing and says nothing.
2. **Map it.** `create_map` with the sources, a title, and any per-source
   `options`. It writes a directory holding `map.html`, `spec.json` and any data
   too large to inline, publishes it on a loopback port, and returns a URL.
3. **Report the URL, not the path.** A `file://` page cannot POST, so it cannot
   save its edits, and Chrome blocks it from fetching a sibling Parquet file —
   so a map whose data is too big to inline does not even render from it. The
   path is worth naming as well, because it is what the user keeps and sends on.
4. **Coming back to a map.** `list_maps` gives the directory names; `open_map`
   takes one, re-renders it against the current viewer, and returns a fresh URL.
   `list_maps` also returns the URL of the **map index** — every local map on
   one page, a card each with a drawing of its data, served from the same
   loopback port at its root. When the user asks what maps they have, give them
   that URL: it is one link that shows all of them, and it needs no account.
5. **Sign in only to upload.** `auth_status` first — it makes no network request
   and never opens a browser, so it is safe to call whenever a hosted call has
   failed. `login` opens a browser and blocks until sign-in finishes. Then
   `list_projects` / `create_project`, then `upload_map` with a local map's
   directory name. `update_server_map` pushes a local map over an existing
   server map, keeping its id, its project and its URL.

**Codex answers a confirmation card itself when it cannot prompt you** — in
`codex exec`, and in a session with approvals bypassed
(`--dangerously-bypass-approvals-and-sandbox`). `delete_map` and
`delete_server_map` ask through elicitation and refuse when no card can be
drawn, returning a line that says so. That is a refusal, not a deletion: put the
question to the user yourself, and only call again with `confirm=True` once they
have agreed to that specific map going.

## What decides whether the map draws anything

The file's columns decide the layer, not its extension.

| What was found | `kind` | Layer built |
| --- | --- | --- |
| A numeric latitude/longitude pair | `point` | `point` |
| A GeoJSON geometry column (`_geojson`) | `geojson` | `geojson` |
| A column of H3 cell ids | `h3` | `hexagonId` |
| Neither | `table` | none |

A `table` dataset is not a failure — it loads, it is listed with all its
columns, and the user picks a layer type — but if the file plainly has
coordinates and `inspect_data` called it a table, that is the moment to say so
and name the columns:

```json
{"*": {"lat": "Lat", "lng": "Lon"}}
```

Coordinates are recognised by name **and** by type. A column of coordinate
strings is not adopted, because a point layer on a VARCHAR column draws nothing.

## What makes a map worth looking at

- **Colour and size are what turn a map into an answer.** A map of points says
  where; a map coloured by a value says what. Use `color_field`, and `size_field`
  where size carries meaning, and name the column you used.
- **Match the scale to the distribution.** `quantile` for skewed data — rates,
  counts, anything long-tailed — which is the default. `ordinal` only for a
  genuinely categorical field; on a numeric column it produces one colour per
  distinct value.
- **Use the aggregating types when the points are unreadable.** `heatmap` for
  concentration, `hexbin` or `grid` for counts per area, `cluster` for thousands
  of points that collapse into a blob.
- **Aggregate before an H3 map.** One row per cell, with the value already
  summed — several rows per cell draws the last one.

## Hosted storage, and what it costs

The server keeps one free map and one free dataset per account; past that it is
pay-as-you-go, metered on the Parquet stored and the bytes served back when the
map is opened. Say so when a user is deciding whether to upload a large dataset
rather than keep it local — reading it locally costs nothing.
