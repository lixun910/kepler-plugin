# Layer types

What `create_map` writes for each `layer_type`, what each one needs in the
dataset, and what to reach for when a user asks for a particular kind of map.
The shapes below are kepler.gl 3.x and are the same ones the plugin writes — if
you hand-edit a saved `spec.json`, keep them.

## The layer object

Every layer has exactly this shape. The single most expensive mistake in a
kepler config is putting `colorField` next to `dataId` — kepler accepts it, and
the layer draws without the ramp and without an error.

```json
{
  "id": "earthquakes-point",
  "type": "point",
  "config": {
    "dataId": "earthquakes",
    "label": "Earthquakes",
    "color": [231, 159, 213],
    "columns": {"lat": "Latitude", "lng": "Longitude", "altitude": null},
    "isVisible": true,
    "visConfig": {"radius": 10, "opacity": 0.8, "filled": true}
  },
  "visualChannels": {
    "colorField": "Magnitude",
    "colorScale": "quantile",
    "strokeColorField": null,
    "strokeColorScale": "quantile",
    "sizeField": "Magnitude",
    "sizeScale": "linear"
  }
}
```

- `config.dataId` must equal the dataset's `id` in `spec.json` — which is the
  table name the plugin derived from the file, not the label.
- `visualChannels.colorField` and `sizeField` are **column names as strings**.
  An object like `{"name": "Magnitude", "type": "real"}` is older kepler.gl and
  is not read.
- `columns` names real columns. A name that is not in the dataset is the silent
  empty-layer failure described in
  [troubleshooting.md](troubleshooting.md).

## Coordinate layers

`point` (default for a `point` dataset), `heatmap`, `grid`, `hexbin`, `cluster`.

Need `columns.lat` and `columns.lng`. Which to choose:

| Ask | Type | Notes |
| --- | --- | --- |
| Show me these locations | `point` | The default, and the right answer most of the time |
| Where is it concentrated | `heatmap` | Density, not counts; good for tens of thousands of points |
| Count per area | `grid` / `hexbin` | Both aggregate; `hexbin` reads better on a projected map |
| Thousands of points, unreadable | `cluster` | Counts collapse into a bubble that splits as you zoom |

Ramps: `point` and `cluster` use the ember ramp, `grid` and `hexbin` the teal
one. `radius` (pixels) applies to `point` and `heatmap`; `sizeRange` to the
aggregating types.

## `geojson` — polygons and lines

Needs `columns.geojson`, which names the `_geojson` column the plugin
materialises. It holds one GeoJSON geometry per row, as a JSON string; the
dataset's other columns are the properties, and they are what the tooltip reads.

A choropleth is a `geojson` layer with a `colorField`:

```json
"visualChannels": {
  "colorField": "unemployment_rate",
  "colorScale": "quantile",
  "strokeColorField": null,
  "strokeColorScale": "quantile",
  "sizeField": null,
  "sizeScale": "linear"
}
```

`colorScale` options, and when each is right:

- `quantile` — equal counts per class. The default, and the honest choice for a
  choropleth of rates, which are almost always skewed.
- `quantize` — equal value ranges. Use when the user is comparing against fixed
  thresholds.
- `ordinal` — for a categorical column. Requires the field to be genuinely
  categorical; on a numeric column kepler produces one colour per distinct
  value, which on a rate column is thousands of colours.

If the user asks for specific class breaks, compute them and add a derived
column to the data rather than trying to express breaks in the config.

## `3d` — extruded polygons

Same columns as `geojson`, plus a height. Set `visualChannels.sizeField` to the
column that sets the height and leave `visConfig.extruded` on. Good for
comparing magnitudes across areas; bad for reading precise values, because
height is harder to compare than colour.

## `hexagonId` — H3 cells

Needs `columns.hex_id` naming a column of H3 cell id strings. The plugin finds
it by name (`h3`, `h3_index`, `h3_id`, `hex_id`, `h3_cell`, `h3index`) and, when
the values look like H3, by value. A column of arbitrary 15-character hex
strings is not a set of cells and will not draw.

Aggregate before mapping: one row per cell, with the value already summed. A
dataset with several rows per cell draws the last one.

## `arc` and `line` — origin to destination

Need four columns, named explicitly through `options`:

```json
{"layer_type": "arc",
 "source_lat": "origin_lat", "source_lng": "origin_lon",
 "target_lat": "dest_lat",   "target_lng": "dest_lon"}
```

`lat0`/`lng0` is the **source** and `lat1`/`lng1` the target; swapping them
draws every arc backwards, which is only visible if the arcs are asymmetric.
`arc` curves and reads as a route; `line` is straight and reads as a link.

## Basemap

`style` is `dark` (default) or `light`, which map to CARTO's `dark-matter` and
`positron`. Both are free and need no Mapbox token, which matters because the
plugin has no way to ask for one. A user who wants a Mapbox basemap has to
export the config and edit `mapStyle` themselves.

## Tooltips

`create_map` puts the first twelve columns of each dataset into the tooltip,
excluding `_geojson`. Tooltips are per dataset, keyed by the dataset id:

```json
"interactionConfig": {
  "tooltip": {
    "enabled": true,
    "fieldsToShow": {
      "earthquakes": [{"name": "Magnitude", "format": null}]
    }
  }
}
```

`fieldsToShow` maps a dataset id to a list of `{name, format}` objects — not a
list of bare strings, which older documentation shows and kepler does not read.
