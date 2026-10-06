"""Building the kepler configuration that the viewer loads.

A rendered map is `{version: 'v1', config: {visState, mapState, mapStyle}}` —
the same object the Save button writes back. Two consequences shape this module.

**The config is written, not left to kepler.** kepler can auto-create a layer
from a dataset, and `addDataToMap` will do it when asked. That is fine for
`kind: 'point'` and useless for the rest: a GeoJSON dataset gets no layer at
all, and an H3 column gets nothing, because kepler has no way to know that a
column of 15-character hex strings is a set of cells. Worse, `autoCreateLayers`
is *off* whenever a config is present — and for a saved map a config always is.
So every layer is written here, explicitly, and `autoCreateLayers` is left to do
nothing.

**Every field a layer names has to exist in the dataset.** A kepler layer whose
`columns.lat` names a column that is not there does not fail: the layer draws
nothing, the map shows a basemap, and no error reaches the console. That is the
single most expensive failure mode in this file, and it is why the lat/lng
column names come from `data.classify` — decided from the data that was actually
loaded — rather than from a convention about what the file probably called them.

The palette entries below are written with `category: 'Custom'` and explicit
colours rather than by naming one of kepler's built-in ranges. A name that does
not resolve to a registered range is accepted silently and the layer falls back
to a single colour, which is a legend that says nothing about the data it is
supposed to describe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .data import GEOJSON_COLUMN, LoadedTable

#: The default viewport, used when nothing better is known. Pointed at the
#: continental United States at a zoom that fits it, which is the least
#: surprising place for a map with no geometry to open.
DEFAULT_VIEWPORT = {"latitude": 37.75, "longitude": -122.45, "zoom": 11}

#: Layer colours, cycled per dataset. Chosen to be distinguishable on the
#: dark-matter basemap the viewer defaults to — kepler's own default blue is
#: nearly invisible against it.
PALETTE = [
    (231, 159, 213),
    (111, 208, 216),
    (255, 153, 31),
    (127, 209, 166),
    (255, 204, 102),
    (144, 173, 255),
    (255, 138, 128),
    (196, 168, 255),
]

#: Named colour ranges for the layers that ramp rather than fill. Written out
#: rather than referenced, for the reason in the module docstring.
RANGES = {
    "ember": {
        "name": "Ember",
        "type": "sequential",
        "category": "Custom",
        "colors": ["#2B1B3D", "#6A2C70", "#B83B5E", "#E9724C", "#FFC857"],
    },
    "teal": {
        "name": "Teal",
        "type": "sequential",
        "category": "Custom",
        "colors": ["#0B2027", "#12556B", "#218C8D", "#4FC1A6", "#B8EBD0"],
    },
    "magenta": {
        "name": "Magenta",
        "type": "sequential",
        "category": "Custom",
        "colors": ["#1B0B2E", "#4A1A6B", "#8B2FA0", "#C85FC4", "#F2B5E0"],
    },
}

#: Which kepler layer types can be built, and what each one needs to exist in
#: the dataset. Consulted before a layer is written, so an impossible
#: combination fails here with a sentence rather than silently on the map.
LAYER_REQUIREMENTS = {
    "point": "latitude and longitude columns",
    "heatmap": "latitude and longitude columns",
    "grid": "latitude and longitude columns",
    "hexbin": "latitude and longitude columns",
    "cluster": "latitude and longitude columns",
    "geojson": "a GeoJSON geometry column",
    "3d": "a GeoJSON geometry column",
    "hexagonId": "an H3 cell id column",
    "arc": "four columns: source and target latitude and longitude",
    "line": "four columns: source and target latitude and longitude",
}

#: The layer type a dataset kind gets when the caller does not choose. `table`
#: maps to None, because there is nothing in it to draw: no coordinate pair, no
#: geometry. It is not a failure — a table still loads, still appears in the
#: layer panel, and still carries every column — so the map opens with the data
#: ready and the user picks a layer type.
DEFAULT_LAYER_TYPE = {
    "point": "point",
    "geojson": "geojson",
    "h3": "hexagonId",
    "table": None,
}

#: visConfig is a partial override of kepler's own defaults, so only the values
#: that are worth differing from are listed. A radius of 10 matches what
#: kepler's own auto-created point layer uses; the ramped layers get a range
#: wide enough that a first glance shows variation.
VIS_CONFIG = {
    "point": {
        "radius": 10,
        "fixedRadius": False,
        "opacity": 0.8,
        "outline": False,
        "thickness": 2,
        "filled": True,
        "billboard": False,
        # The range only applies once a colour field is set — kepler uses the
        # single `config.color` until then — so having one here is free, and
        # the alternative is kepler's own default ramp, whose low end is dark
        # enough to disappear into the dark-matter basemap.
        "colorRange": RANGES["ember"],
        "radiusRange": [2, 30],
    },
    "heatmap": {"radius": 20, "opacity": 0.6, "colorRange": RANGES["ember"]},
    "grid": {"coverage": 1, "opacity": 0.8, "sizeRange": [0, 100], "colorRange": RANGES["teal"]},
    "hexbin": {"coverage": 1, "opacity": 0.8, "sizeRange": [0, 100], "colorRange": RANGES["teal"]},
    "cluster": {"clusterRadius": 40, "opacity": 0.8, "colorRange": RANGES["ember"]},
    "geojson": {
        "opacity": 0.7,
        "stroke": True,
        "strokeWidth": 1,
        "strokeOpacity": 0.9,
        "filled": True,
        "stroked": True,
        "extruded": False,
        "wireframe": False,
        "elevationScale": 1,
    },
    "3d": {
        "opacity": 0.8,
        "stroke": False,
        "extruded": True,
        "wireframe": False,
        "elevationScale": 5,
        "enableElevationZoomFactor": True,
        "heightRange": [0, 500],
        "sizeRange": [0, 100],
        "coverage": 1,
    },
    "hexagonId": {
        "coverage": 1,
        "sizeRange": [0, 100],
        "opacity": 0.8,
        "elevationScale": 1,
        "enableElevationZoomFactor": True,
        "colorRange": RANGES["teal"],
    },
    "arc": {
        "opacity": 0.8,
        "thickness": 2,
        "sizeRange": [0, 10],
        "targetColor": [255, 255, 255, 255],
        "strokeColorRange": RANGES["magenta"],
    },
    "line": {"opacity": 0.8, "thickness": 2, "sizeRange": [0, 10], "colorRange": RANGES["magenta"]},
}


@dataclass
class LayerOptions:
    """What the caller asked for, with the defaults applied."""

    layer_type: str | None = None
    label: str | None = None
    color_field: str | None = None
    size_field: str | None = None
    color_scale: str = "quantile"
    size_scale: str = "linear"
    opacity: float | None = None
    radius: float | None = None
    visible: bool = True
    #: For arc and line layers: the four columns, in the order kepler wants.
    source_lat: str | None = None
    source_lng: str | None = None
    target_lat: str | None = None
    target_lng: str | None = None


def build_layer(
    loaded: LoadedTable,
    *,
    index: int = 0,
    options: LayerOptions | None = None,
) -> dict[str, Any] | None:
    """One entry of `visState.layers`, or None when there is nothing to draw.

    Returns None rather than raising for the `table` case, because a dataset
    with no geometry is a legitimate thing to load — the columns are useful in
    the tooltip and the panel — and refusing the whole map over it would be
    worse than opening it without a layer.
    """
    options = options or LayerOptions()
    layer_type = (options.layer_type or DEFAULT_LAYER_TYPE.get(loaded.kind) or "").strip()

    if not layer_type:
        return None
    if layer_type not in LAYER_REQUIREMENTS:
        raise ValueError(
            f"Unknown layer type {layer_type!r}. Available: "
            f"{', '.join(sorted(LAYER_REQUIREMENTS))}."
        )

    columns = _columns_for(layer_type, loaded, options)
    vis_config = dict(VIS_CONFIG.get(layer_type, {}))
    if options.opacity is not None:
        vis_config["opacity"] = options.opacity
    if options.radius is not None and layer_type in {"point", "heatmap"}:
        vis_config["radius"] = options.radius

    color = PALETTE[index % len(PALETTE)]
    label = options.label or loaded.table

    return {
        # kepler's own layer ids are six random characters. This one is derived
        # from the table and the type so that a layer can be recognised in a
        # saved config, and so re-running the same build produces the same
        # config rather than a diff for a map nobody edited.
        "id": f"{loaded.table}-{layer_type}",
        "type": layer_type,
        "config": {
            "dataId": loaded.table,
            "label": label,
            "color": list(color),
            "columns": columns,
            "isVisible": options.visible,
            "visConfig": vis_config,
        },
        "visualChannels": {
            "colorField": _check_field(options.color_field, loaded, "color"),
            "colorScale": options.color_scale,
            "strokeColorField": None,
            "strokeColorScale": "quantile",
            "sizeField": _check_field(options.size_field, loaded, "size"),
            "sizeScale": options.size_scale,
        },
    }


def _columns_for(
    layer_type: str, loaded: LoadedTable, options: LayerOptions
) -> dict[str, Any]:
    """The `columns` block, checked against the dataset's actual columns.

    This is where a wrong assumption becomes an error instead of an empty layer.
    Each branch either produces names that are known to exist or raises naming
    what was found, and the error text is the only place the caller can learn
    which columns the file really had without loading it again.
    """
    names = loaded.column_names
    if layer_type in {"point", "heatmap", "grid", "hexbin", "cluster"}:
        if not (loaded.lat_column and loaded.lng_column):
            raise ValueError(
                f"A {layer_type} layer needs latitude and longitude, and "
                f"{loaded.source!r} was loaded as a {loaded.kind} dataset with no "
                f"coordinate columns. Its columns are: {', '.join(names)}."
            )
        return {"lat": loaded.lat_column, "lng": loaded.lng_column, "altitude": None}

    if layer_type in {"geojson", "3d"}:
        if GEOJSON_COLUMN not in names:
            raise ValueError(
                f"A {layer_type} layer needs a geometry column, and {loaded.source!r} "
                f"has none. Load a GeoJSON file, or a file with a WKT column, or say "
                f"kind='geojson' when loading it."
            )
        return {"geojson": GEOJSON_COLUMN}

    if layer_type == "hexagonId":
        column = _find_h3(loaded)
        if column is None:
            raise ValueError(
                f"A hexagonId layer needs an H3 cell id column, and {loaded.source!r} "
                f"has none. Its columns are: {', '.join(names)}."
            )
        return {"hex_id": column}

    if layer_type in {"arc", "line"}:
        missing = [
            name
            for name, value in (
                ("source_lat", options.source_lat),
                ("source_lng", options.source_lng),
                ("target_lat", options.target_lat),
                ("target_lng", options.target_lng),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"An {layer_type} layer needs all four coordinates. Given "
                f"lat0={options.source_lat!r}, lng0={options.source_lng!r}, "
                f"lat1={options.target_lat!r}, lng1={options.target_lng!r}; missing "
                f"{', '.join(missing)}. The dataset's columns are: {', '.join(names)}."
            )
        # kepler names these lat0/lng0/lat1/lng1 — the *source* end is 0 and the
        # target is 1, and getting the two swapped draws every arc backwards.
        return {
            "lat0": options.source_lat,
            "lng0": options.source_lng,
            "lat1": options.target_lat,
            "lng1": options.target_lng,
        }

    raise ValueError(f"Unhandled layer type {layer_type!r}.")


def _check_field(name: str | None, loaded: LoadedTable, role: str) -> str | None:
    """Reject a colour/size field that is not in the dataset.

    kepler accepts an unknown field name and then draws the layer without the
    ramp, so the mistake shows up as "the colouring did not apply" rather than
    as anything a user could act on.
    """
    if not name:
        return None
    if name not in loaded.column_names:
        raise ValueError(
            f"The {role} field {name!r} is not a column of {loaded.source!r}. "
            f"Its columns are: {', '.join(loaded.column_names)}."
        )
    return name


def _find_h3(loaded: LoadedTable) -> str | None:
    lowered = {c.name.lower(): c.name for c in loaded.columns}
    for candidate in ("h3", "h3_index", "h3_id", "hex_id", "h3_cell", "h3index"):
        if candidate in lowered:
            return lowered[candidate]
    return None


def build_tooltips(tables: list[LoadedTable]) -> dict[str, list[dict[str, Any]]]:
    """The fields shown on hover, per dataset.

    Every column, capped. A map whose tooltip shows nothing is a map the user
    has to open the data table to read, and the cap exists because a dataset of
    two hundred columns would produce a tooltip taller than the map. `_geojson`
    is excluded — it is a geometry, not a fact about the row.
    """
    fields: dict[str, list[dict[str, Any]]] = {}
    for loaded in tables:
        shown = [
            {"name": c.name, "format": None}
            for c in loaded.columns
            if c.name != GEOJSON_COLUMN
        ][:12]
        fields[loaded.table] = shown
    return fields


def build_vis_state(
    layers: list[dict[str, Any]],
    tables: list[LoadedTable],
) -> dict[str, Any]:
    """The `visState` block, complete enough to be saved back unchanged.

    The empty collections are not optional decoration. `KeplerGlSchema` reads
    this object when the user saves, and a `visState` missing a key it expects
    produces a config that loads into the viewer with the missing part silently
    defaulted — which is how a saved tooltip configuration disappears.
    """
    return {
        "filters": [],
        "layers": layers,
        "effects": [],
        "interactionConfig": {
            "tooltip": {
                "fieldsToShow": build_tooltips(tables),
                "compareMode": False,
                "compareType": "absolute",
                "enabled": True,
            },
            "brush": {"size": 0.5, "enabled": False},
            "geocoder": {"enabled": False},
            "coordinate": {"enabled": False},
        },
        "layerBlending": "normal",
        "overlayBlending": "normal",
        "splitMaps": [],
        "animationConfig": {"currentTime": None, "speed": 1},
        "editor": {"features": [], "visible": True},
    }


def build_map_state(
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    zoom: float | None = None,
    pitch: float = 0.0,
    bearing: float = 0.0,
) -> dict[str, Any]:
    """The `mapState` block: where the camera starts.

    Only ever the *starting* view. When a map is centred on its data kepler
    overrides all of this on load, and when a saved config is being re-opened
    the config's own `mapState` replaces it wholesale — so this is the fallback
    for the case where neither happens, which is a map with no geometry.
    """
    return {
        "latitude": DEFAULT_VIEWPORT["latitude"] if latitude is None else latitude,
        "longitude": DEFAULT_VIEWPORT["longitude"] if longitude is None else longitude,
        "zoom": DEFAULT_VIEWPORT["zoom"] if zoom is None else zoom,
        "pitch": pitch,
        "bearing": bearing,
        "dragRotate": False,
        "isSplit": False,
        "isViewportSynced": True,
        "isZoomLocked": False,
        "splitMapViewports": [],
    }


def build_map_style(style: str = "dark") -> dict[str, Any]:
    """The `mapStyle` block.

    `styleType` is kepler's name for a basemap, and `dark-matter` / `positron`
    are the two that need no Mapbox token — they are CARTO's maplibre styles,
    fetched from `basemaps.cartocdn.com`. Anything else would require the user
    to hold a Mapbox token, which this plugin has no way to ask for.

    `border` is off by default: the dark-matter style draws country borders in a
    grey that reads as data on a map that is mostly points, and it is one click
    to turn back on.
    """
    style_type = "dark-matter" if style != "light" else "positron"
    return {
        "styleType": style_type,
        "topLayerGroups": {},
        "visibleLayerGroups": {
            "label": True,
            "road": True,
            "border": False,
            "building": True,
            "water": True,
            "land": True,
            "3d building": False,
        },
        "threeDBuildingColor": [15.0, 15.0, 15.0],
        "backgroundColor": [0, 0, 0],
        "mapStyles": {},
    }


def build_config(
    *,
    layers: list[dict[str, Any]],
    tables: list[LoadedTable],
    map_state: dict[str, Any] | None = None,
    style: str = "dark",
) -> dict[str, Any]:
    """The whole saved-config object, in the shape the viewer round-trips."""
    return {
        "version": "v1",
        "config": {
            "visState": build_vis_state(layers, tables),
            "mapState": map_state or build_map_state(),
            "mapStyle": build_map_style(style),
        },
    }


def describe_layers(layers: list[dict[str, Any]]) -> str:
    """A one-line summary, for the report a tool returns."""
    if not layers:
        return "no layers"
    parts = []
    for layer in layers:
        label = layer["config"].get("label") or layer["id"]
        parts.append(f"{label} ({layer['type']})")
    return ", ".join(parts)
