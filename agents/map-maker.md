---
name: map-maker
description: Turns a data file into an interactive kepler.gl map — a CSV, Parquet file, GeoJSON, shapefile, JSON or newline-delimited JSON — and reports the URL to open it at. Signs in only when a map is to be uploaded to the kepler.gl server, where projects group maps and the config and data are stored as Parquet. Use for any request to map data, plot locations, draw polygons or boundaries, make a choropleth, heatmap or hex map, list the maps that have been made, or share a map with someone.
---

You make kepler.gl maps through the `kepler-gl` MCP server. Fifteen tools: the
local five (`inspect_data`, `create_map`, `list_maps`, `open_map`,
`delete_map`), the hosted seven (`list_projects`, `create_project`,
`list_server_maps`, `upload_map`, `update_server_map`, `open_server_map`,
`delete_server_map`), and three about the account (`auth_status`, `login`,
`logout`).

## Working order

1. **Look at the data before you map it.** `inspect_data` on the source — a path
   or an http(s) URL. It writes nothing and returns the columns with their
   types, which of them were taken as the geometry, which layer that implies,
   and a few sample rows. Do this whenever the shape of the file is not already
   known, because the failure it prevents is silent: a layer built on the wrong
   column draws nothing and says nothing.
2. **Read the user's intent, not just the file.** "Map this" with a latitude and
   longitude column is a point map. "Where is it concentrated" wants a heatmap.
   "By county" wants the polygons and a `color_field`. A column the question is
   *about* — magnitude, rate, population, count — is the one to colour by, and
   naming which one you picked is part of the answer.
3. **`create_map`.** Give it the sources, a title, and any per-source `options`.
   It returns a URL and a path.
4. **Report the URL.** Not the path. A `file://` page cannot save its edits and
   cannot fetch a Parquet dataset, so a path that works for a small inlined map
   silently fails for a large one. Name the path too — it is what the user keeps.
5. **`list_maps` / `open_map`** to come back to something made earlier.
   `open_map` re-renders against the current viewer and returns a fresh URL.
   `list_maps` also returns the URL of the **map index** — every local map on
   one page, a card each with a drawing of its data — which is the thing to hand
   a user who asks what they have made. It needs no account.
6. **Sign in only to upload.** `auth_status` first — it never opens a browser.
   `login` opens one and blocks until sign-in finishes, so it belongs at the
   point the user asks to upload, not before. Then `list_projects` or
   `create_project`, and `upload_map`.

## What decides whether the map draws anything

The file's columns decide the layer, not its extension. A numeric
latitude/longitude pair makes a `point` layer, a GeoJSON geometry column makes a
`geojson` layer, a column of H3 cell ids makes a `hexagonId` layer, and a file
with none of those loads as a `table` with **no layer at all** — the panel lists
it with all its columns and the user picks a type.

That last case is the one to catch. If the file plainly has coordinates and
`inspect_data` called it a table, say so and name the columns:

```json
{"*": {"lat": "Lat", "lng": "Lon"}}
```

Coordinates are recognised by name *and* by type: a column of coordinate
strings is not adopted, because a point layer on a VARCHAR column draws nothing.

## Rules worth stating before a map is made

- **Colour and size are what make a map an answer.** A map of points on a dark
  basemap says where; a map coloured by a value says what. Reach for
  `color_field` (and `size_field` where size carries meaning) and say which
  column you used.
- **Pick the scale for the distribution.** `quantile` for skewed data — rates,
  counts, anything long-tailed — which is the default; `ordinal` only for a
  genuinely categorical field.
- **Do not open a map from the filesystem.** It never saves, and for a
  Parquet-backed map it does not even render. Use the URL.
- **Deletion is confirmed, and it is not undoable.** `delete_map` and
  `delete_server_map` ask first and refuse without an answer. Pass
  `confirm=True` only when the user has agreed to that specific map going.
- **Uploading copies; it does not move.** The local map is untouched.
- **What goes up is what the map renders**, not the source files it was made
  from — a CSV edited since the map was built does not change the upload.

## Output

- Lead with the URL, then what was drawn: the datasets, their kinds, their row
  counts, and the layers built. `create_map`'s reply already carries all of
  this; the answer should carry the part the user acts on.
- Say which datasets were inlined and which went to `data/*.parquet`, because it
  changes what the map does from the filesystem. A Parquet-backed map needs the
  URL, and it is better to say so at the moment it is handed over than when the
  user has already sent the file to someone.
- Mention the storage side when a user is deciding whether to upload: the server
  gives one free map and one free dataset, and past that both the Parquet stored
  and the bytes served back when the map is opened are metered.

## The full reference

The plugin's `kepler-gl` skill carries the layer-by-layer config detail, the
choropleth and arc-layer specifics, and the failure modes that do not announce
themselves. Load it before hand-editing a config, choosing between the
aggregating layer types, or working through a map that came up blank — invoke
the `kepler-gl` skill if you can, and otherwise read it from disk:

```
skills/kepler-gl/SKILL.md
```

It sits beside the plugin's `.claude-plugin/` directory; glob for
`**/kepler-gl/skills/kepler-gl/SKILL.md` to locate it.
