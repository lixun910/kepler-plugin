"""The local map index: every map on this machine, as one page.

A user who has made four maps has four directories they have to remember the
names of. `list_maps` answers that in text, which is right for a model reading
the answer and wrong for the person who wants to see their maps. This module is
the page version: a grid of cards, each one a link to its map, each showing a
thumbnail of what is in it.

Three decisions shape it.

**The page is generated per request, never written to disk.** The alternative —
an `index.html` beside the maps, regenerated whenever a map changes — is the
index file `store.py` already argues against, for the same reason: a second copy
of the truth drifts from the directories the moment someone deletes a map in
Finder or copies one in from a colleague. The preview server renders the page
from a fresh scan of `~/kepler-maps`, so what is listed and what is on disk
cannot disagree.

**The thumbnail is a drawing, not an iframe.** The obvious preview is the map
itself in a small frame. It is the wrong one: every frame loads the 13 MB bundle
and asks for its own WebGL context, browsers cap those at around sixteen, and a
grid of twenty maps turns into a page that half-renders with no error anywhere.
Drawing the geometry into an SVG instead costs nothing, needs no JavaScript,
works from any browser, and shows the thing a thumbnail is for — the shape of
the data. A map with nothing drawable gets an honest placeholder naming what it
does have, not an empty frame.

**Colors come from the config, not from a fresh guess.** The thumbnail draws
each dataset in the colour its own layer uses, so the card looks like the map
behind it. Reading the colour back out of the spec is what keeps the two in
step after a user recolours a layer and saves.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Sequence

from .data import GEOJSON_COLUMN
from .layers import PALETTE
from .maps import escape_html
from .store import LocalMap

#: The thumbnail's coordinate space, in SVG user units. The card scales it with
#: CSS, so these only have to be in the right proportion — 16:9, matching the
#: card's thumbnail box.
PREVIEW_WIDTH = 480
PREVIEW_HEIGHT = 270

#: How much geometry a thumbnail will draw. A map can inline two hundred
#: thousand rows and a browser asked to paint two hundred thousand circles turns
#: a gallery into a stall. Sampling by stride keeps the shape of a dense dataset
#: — which is all a thumbnail is for — while keeping the page cheap.
MAX_DRAW_POINTS = 1500
MAX_DRAW_VERTICES = 3000

#: Padding inside the thumbnail, so points on the edge of the data's extent are
#: not drawn half off the card.
_PAD = 14

#: A drawable point: longitude, latitude, and the CSS colour of its layer.
DrawPoint = tuple[float, float, str]

#: A drawable path: its ring of `(lon, lat)` vertices, its colour, and whether
#: it closes — a polygon — or not, a line. Named for what it is rather than
#: `Path`, which `pathlib` already owns in this module.
DrawPath = tuple[list[tuple[float, float]], str, bool]

#: Column names the plugin would have taken as coordinates, in the order it
#: prefers them. Consulted only when a point dataset has no layer to read the
#: names back from — a dataset loaded as `table`, or a map saved before a layer
#: was added.
_LAT_NAMES = ("latitude", "lat", "y")
_LNG_NAMES = ("longitude", "lon", "lng", "long", "x")


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def render_index(
    maps: Sequence[LocalMap],
    *,
    root: Path,
    bundle_present: bool = True,
) -> str:
    """The whole index page for a list of maps.

    `maps` arrives already sorted, newest first, straight from
    `LocalStore.list_maps`. Nothing here touches the filesystem: this is a pure
    function of the list, which is what makes it testable without a maps
    directory on the machine.
    """
    cards = "\n".join(_card(record) for record in maps)
    count = len(maps)
    subtitle = (
        f"{count} map{'s' if count != 1 else ''} in <code>{escape_html(root)}</code>"
        if maps
        else f"Nothing in <code>{escape_html(root)}</code> yet"
    )

    notice = ""
    if maps and not bundle_present:
        # The maps will not render without it, and the cards would look fine.
        notice = (
            '<p class="notice">The viewer bundle <code>kepler-viewer.js</code> is '
            "not in this directory, so these maps will not load. Run "
            "<code>open_map</code> once to place it.</p>"
        )

    body = (
        f'<div class="grid">\n{cards}\n</div>'
        if maps
        else _empty_state()
    )

    return _PAGE.format(
        subtitle=subtitle,
        notice=notice,
        body=body,
        count=count,
    )


def _empty_state() -> str:
    """What the page says when there are no maps.

    Worth writing out rather than leaving blank: a user who opens this page has
    almost certainly just been told their maps are here, and an empty grid reads
    as a broken page rather than as an empty directory.
    """
    return (
        '<div class="empty">'
        "<h2>No maps yet</h2>"
        "<p>Ask the plugin for one — <code>create_map</code> takes a CSV, a "
        "Parquet file, a GeoJSON file, a shapefile or a URL, and writes the map "
        "here. It needs no account.</p>"
        "<p class=\"hint\">Map directories appear on this page as they are "
        "made. Nothing has to be refreshed by hand.</p>"
        "</div>"
    )


def _card(record: LocalMap) -> str:
    """One map, as a card."""
    slug = escape_html(record.slug)
    title = escape_html(record.title)
    href = f"/{slug}/map.html"

    description = ""
    if record.description:
        description = f'<p class="desc">{escape_html(record.description)}</p>'

    meta = _meta_line(record)
    datasets = escape_html(_dataset_summary(record))

    return f"""<article class="card">
  <a class="thumb" href="{href}" title="Open {title}">
    {preview_svg(record)}
  </a>
  <div class="body">
    <h2><a href="{href}">{title}</a></h2>
    {description}
    <p class="meta">{meta}</p>
    <p class="datasets">{datasets}</p>
    <div class="actions">
      <a class="primary" href="{href}">Open map</a>
      <a class="quiet" href="/{slug}/spec.json">spec.json</a>
      <span class="slug" title="The map's directory name, for open_map">{slug}</span>
    </div>
  </div>
</article>"""


def _meta_line(record: LocalMap) -> str:
    """`2 datasets · 2 layers · edited 18 Sep 2026, 16:10`."""
    parts = [f"{len(record.datasets)} dataset{'s' if len(record.datasets) != 1 else ''}"]
    layers = _layers(record.spec)
    if layers:
        parts.append(f"{len(layers)} layer{'s' if len(layers) != 1 else ''}")
    when = record.updated_at()
    if when is not None:
        # `updated_at` is UTC-aware; the reader is in their own timezone.
        parts.append(f"edited {when.astimezone().strftime('%d %b %Y, %H:%M')}")
    return escape_html(" · ".join(parts))


def _dataset_summary(record: LocalMap) -> str:
    """The dataset labels with their kinds, capped so a card stays a card."""
    labels = []
    for dataset in record.datasets:
        if not isinstance(dataset, dict):
            continue
        label = dataset.get("label") or dataset.get("id") or "dataset"
        kind = dataset.get("kind")
        labels.append(f"{label} ({kind})" if kind else str(label))
    if not labels:
        return "no datasets"
    if len(labels) > 3:
        return ", ".join(labels[:3]) + f", +{len(labels) - 3} more"
    return ", ".join(labels)


# ---------------------------------------------------------------------------
# The thumbnail
# ---------------------------------------------------------------------------


def preview_svg(record: LocalMap) -> str:
    """A drawing of a map's geometry, sized for the card.

    Returns an `<svg>` element. Falls back to a labelled placeholder rather than
    an empty frame when the spec holds nothing this can draw — an H3 dataset, a
    `table`, or a dataset held in Parquet, none of which carry coordinates in
    the spec.
    """
    spec = record.spec
    theme = spec.get("theme") or "dark"
    light = theme == "light"
    background = "#f4f5f7" if light else "#12141a"
    graticule = "#e4e7ec" if light else "#1b1f27"
    placeholder_fill = "#e7eaee" if light else "#1b1f27"
    caption_fill = "#7a8494" if light else "#6b7688"

    points, paths = _geometry(record)
    drawn = len(points) + sum(len(ring) for ring, _, _ in paths)

    projector = _projector(points, paths) if drawn else None
    if projector is None:
        return _placeholder(record, background, placeholder_fill, caption_fill)

    parts = [
        f'<svg class="preview" viewBox="0 0 {PREVIEW_WIDTH} {PREVIEW_HEIGHT}" '
        f'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">',
        f'<rect width="{PREVIEW_WIDTH}" height="{PREVIEW_HEIGHT}" fill="{background}"/>',
    ]
    parts.extend(_graticule(graticule))

    # Polygons first, so points drawn on top of a filled area stay visible.
    for ring, color, closed in paths:
        pts = " ".join(
            f"{x:.1f},{y:.1f}" for x, y in (projector(lon, lat) for lon, lat in ring)
        )
        if closed:
            parts.append(
                f'<polygon points="{pts}" fill="{color}" fill-opacity="0.28" '
                f'stroke="{color}" stroke-width="1" stroke-opacity="0.9"/>'
            )
        else:
            parts.append(
                f'<polyline points="{pts}" fill="none" stroke="{color}" '
                f'stroke-width="1.2" stroke-opacity="0.9"/>'
            )

    for lon, lat, color in points:
        x, y = projector(lon, lat)
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.7" fill="{color}"/>')

    parts.append("</svg>")
    return "".join(parts)


def _placeholder(
    record: LocalMap, background: str, fill: str, caption_fill: str
) -> str:
    """A frame for a map whose data cannot be drawn from the spec alone.

    Says which dataset kinds are involved, so the card is informative rather
    than merely decorative: `hexagonId (200,050 rows)` tells the user more than
    an empty rectangle does, and it is the truth — the map does have data, it
    just lives in `data/*.parquet` and not in the spec.
    """
    kinds = []
    for dataset in record.datasets:
        if not isinstance(dataset, dict):
            continue
        kind = dataset.get("kind") or "dataset"
        if kind not in kinds:
            kinds.append(str(kind))
    caption = ", ".join(kinds) if kinds else "no geometry"
    # An H3 cell id is not a coordinate, and a Parquet-backed dataset keeps its
    # rows out of the spec entirely. Both are worth naming rather than hiding
    # behind "no preview".
    reason = (
        "held in data/*.parquet" if _parquet_backed(record) else "no drawable coordinates"
    )
    return (
        f'<svg class="preview" viewBox="0 0 {PREVIEW_WIDTH} {PREVIEW_HEIGHT}" '
        f'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
        f'<rect width="{PREVIEW_WIDTH}" height="{PREVIEW_HEIGHT}" fill="{background}"/>'
        f'<rect x="16" y="16" width="{PREVIEW_WIDTH - 32}" '
        f'height="{PREVIEW_HEIGHT - 32}" rx="10" fill="{fill}" '
        f'stroke="{caption_fill}" stroke-opacity="0.35" stroke-dasharray="5 5"/>'
        f'<text x="{PREVIEW_WIDTH // 2}" y="{PREVIEW_HEIGHT // 2 - 6}" '
        f'text-anchor="middle" font-family="-apple-system, BlinkMacSystemFont, '
        f'Segoe UI, Helvetica, Arial, sans-serif" font-size="22" font-weight="600" '
        f'fill="{caption_fill}">{escape_html(caption)}</text>'
        f'<text x="{PREVIEW_WIDTH // 2}" y="{PREVIEW_HEIGHT // 2 + 20}" '
        f'text-anchor="middle" font-family="-apple-system, BlinkMacSystemFont, '
        f'Segoe UI, Helvetica, Arial, sans-serif" font-size="14" '
        f'fill="{caption_fill}">{escape_html(reason)}</text>'
        "</svg>"
    )


def _graticule(color: str) -> list[str]:
    """Faint quarter lines behind the geometry.

    A thumbnail whose data happens to be a single line — a coast, a flight path,
    a transect — is otherwise mostly empty card, and empty reads as broken. Four
    lines cost nothing and make the frame look intentional, which is the whole
    of what a graticule does on a map this small. No labels: nothing here knows
    what the extent is in degrees, and a made-up one would be a lie.
    """
    lines = []
    for step in (1, 2, 3):
        x = PREVIEW_WIDTH * step / 4
        y = PREVIEW_HEIGHT * step / 4
        lines.append(
            f'<line x1="{x:.0f}" y1="0" x2="{x:.0f}" y2="{PREVIEW_HEIGHT}" '
            f'stroke="{color}" stroke-width="1"/>'
        )
        lines.append(
            f'<line x1="0" y1="{y:.0f}" x2="{PREVIEW_WIDTH}" y2="{y:.0f}" '
            f'stroke="{color}" stroke-width="1"/>'
        )
    return lines


# ---------------------------------------------------------------------------
# Reading geometry back out of a spec
# ---------------------------------------------------------------------------


def _geometry(record: LocalMap) -> tuple[list[DrawPoint], list[DrawPath]]:
    """Every drawable coordinate in a map, with the colour of its layer.

    Returns points as `(lon, lat, color)` and paths as `(ring, color, closed)`,
    which is exactly what the SVG writer needs and nothing more. Only datasets
    inlined in the spec contribute — a Parquet-backed one has its rows in a file
    beside the map, and reading that to draw a 480-pixel thumbnail would cost
    more than the thumbnail is worth.
    """
    spec = record.spec
    colors = _layer_colors(spec)
    columns = _coordinate_columns(spec)

    points: list[DrawPoint] = []
    paths: list[DrawPath] = []

    for index, dataset in enumerate(record.datasets):
        if not isinstance(dataset, dict):
            continue
        rows = dataset.get("rows")
        if not isinstance(rows, list) or not rows:
            continue

        color = _rgb(colors.get(dataset.get("id")) or PALETTE[index % len(PALETTE)])
        kind = dataset.get("kind")

        if kind == "geojson":
            _collect_geojson(rows, color, points, paths)
            continue

        pair = columns.get(dataset.get("id")) or _infer_columns(rows[0])
        if pair:
            _collect_points(rows, pair, color, points)

    return points, paths


def _layer_colors(spec: dict[str, Any]) -> dict[str, tuple[int, int, int]]:
    """`dataId -> rgb`, from the layers the config actually saved."""
    colors: dict[str, tuple[int, int, int]] = {}
    for layer in _layers(spec):
        config = layer.get("config") or {}
        data_id = config.get("dataId")
        color = config.get("color")
        if not data_id or not isinstance(color, (list, tuple)) or len(color) < 3:
            continue
        try:
            colors[str(data_id)] = (int(color[0]), int(color[1]), int(color[2]))
        except (TypeError, ValueError):
            continue
    return colors


def _coordinate_columns(spec: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """`dataId -> (lat, lng)` for the layers that name a coordinate pair."""
    columns: dict[str, tuple[str, str]] = {}
    for layer in _layers(spec):
        config = layer.get("config") or {}
        names = config.get("columns") or {}
        lat, lng = names.get("lat"), names.get("lng")
        data_id = config.get("dataId")
        if data_id and lat and lng:
            columns[str(data_id)] = (str(lat), str(lng))
    return columns


def _layers(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """The saved layers, dug out of the nested config shape.

    A spec is `{mapId, ..., config: {version, config: {visState, mapState,
    mapStyle}}}` — the same object kepler round-trips — so the layers are two
    `config` keys down. Each step is guarded because a map that was created and
    never edited has no config at all, and a map edited by hand may have a
    shape this does not expect.
    """
    outer = spec.get("config")
    if not isinstance(outer, dict):
        return []
    inner = outer.get("config")
    if not isinstance(inner, dict):
        return []
    vis = inner.get("visState")
    if not isinstance(vis, dict):
        return []
    layers = vis.get("layers")
    if not isinstance(layers, list):
        return []
    return [layer for layer in layers if isinstance(layer, dict)]


def _infer_columns(row: Any) -> tuple[str, str] | None:
    """Coordinate columns guessed from a row's own keys.

    The last resort, for a dataset with no layer to read the names from. It is
    the same name-based guess `data.classify` makes, and it is deliberately
    narrow: a wrong guess here draws nothing, which is the placeholder's job
    anyway.
    """
    if not isinstance(row, dict):
        return None
    lowered = {str(key).lower(): key for key in row}
    lat = next((lowered[name] for name in _LAT_NAMES if name in lowered), None)
    lng = next((lowered[name] for name in _LNG_NAMES if name in lowered), None)
    return (str(lat), str(lng)) if lat is not None and lng is not None else None


def _numeric(value: Any) -> float | None:
    """A finite float, or None. Booleans are not coordinates."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _collect_points(
    rows: list[Any],
    columns: tuple[str, str],
    color: str,
    points: list[DrawPoint],
) -> None:
    """Sample a point dataset into `points`, at most `MAX_DRAW_POINTS` of them."""
    lat_name, lng_name = columns
    step = max(1, len(rows) // MAX_DRAW_POINTS)
    taken = 0
    for index in range(0, len(rows), step):
        if taken >= MAX_DRAW_POINTS:
            break
        row = rows[index]
        if not isinstance(row, dict):
            continue
        lat = _numeric(row.get(lat_name))
        lng = _numeric(row.get(lng_name))
        if lat is None or lng is None:
            continue
        points.append((lng, lat, color))
        taken += 1


def _collect_geojson(
    rows: list[Any],
    color: str,
    points: list[DrawPoint],
    paths: list[DrawPath],
) -> None:
    """Turn `_geojson` geometries into rings and points.

    Only the outer ring of a polygon is drawn. Holes would be a second SVG path
    per feature and a fill rule to go with it, and at 480 pixels across the
    difference is not visible — the thumbnail is for recognising a map, not for
    reading it.
    """
    for row in rows:
        if not isinstance(row, dict):
            continue
        geometry = row.get(GEOJSON_COLUMN)
        if isinstance(geometry, str):
            try:
                geometry = json.loads(geometry)
            except (ValueError, TypeError):
                continue
        if not isinstance(geometry, dict):
            continue
        _walk_geometry(geometry, color, points, paths, depth=0)


def _walk_geometry(
    geometry: dict[str, Any],
    color: str,
    points: list[DrawPoint],
    paths: list[DrawPath],
    *,
    depth: int,
) -> None:
    """Recurse a GeoJSON geometry into rings and points."""
    if depth > 4:
        return
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")

    if kind == "GeometryCollection":
        for child in geometry.get("geometries") or []:
            if isinstance(child, dict):
                _walk_geometry(child, color, points, paths, depth=depth + 1)
        return

    if kind == "Point":
        point = _pair(coordinates)
        if point:
            points.append((point[0], point[1], color))
        return

    if kind == "MultiPoint":
        for item in coordinates or []:
            point = _pair(item)
            if point:
                points.append((point[0], point[1], color))
        return

    if kind == "LineString":
        ring = _ring(coordinates)
        if len(ring) >= 2:
            paths.append((ring, color, False))
        return

    if kind == "MultiLineString":
        for line in coordinates or []:
            ring = _ring(line)
            if len(ring) >= 2:
                paths.append((ring, color, False))
        return

    if kind == "Polygon":
        ring = _ring(coordinates[0] if coordinates else None)
        if len(ring) >= 3:
            paths.append((ring, color, True))
        return

    if kind == "MultiPolygon":
        for polygon in coordinates or []:
            ring = _ring(polygon[0] if polygon else None)
            if len(ring) >= 3:
                paths.append((ring, color, True))


def _pair(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    lng = _numeric(value[0])
    lat = _numeric(value[1])
    return (lng, lat) if lng is not None and lat is not None else None


def _ring(value: Any) -> list[tuple[float, float]]:
    """A coordinate list as `(lon, lat)` pairs, sampled to a vertex budget."""
    if not isinstance(value, (list, tuple)):
        return []
    step = max(1, len(value) // MAX_DRAW_VERTICES)
    ring = []
    for index in range(0, len(value), step):
        point = _pair(value[index])
        if point:
            ring.append(point)
    # The closing vertex is dropped by striding, and a polygon drawn without it
    # has a side missing. Put it back.
    last = _pair(value[-1]) if value else None
    if last and ring and ring[-1] != last:
        ring.append(last)
    return ring


# ---------------------------------------------------------------------------
# Projecting lon/lat into the thumbnail
# ---------------------------------------------------------------------------


def _projector(points: list[DrawPoint], paths: list[DrawPath]):
    """A function from `(lon, lat)` to `(x, y)` in the thumbnail, or None.

    Equirectangular with the longitude axis compressed by `cos(latitude)` — the
    cheapest projection that does not make a map of the United States twice as
    wide as it is. Not correct at the poles and not meant to be: the thumbnail
    is for recognising the shape of a dataset, and a projection that needed a
    library would be a dependency this page does not earn.

    The data's own extent is the frame, so a dense city dataset and a global one
    both fill the card. That is the right choice for a thumbnail and the wrong
    one for a map, which the click through to the map provides.
    """
    lons: list[float] = []
    lats: list[float] = []
    for lon, lat, _ in points:
        lons.append(lon)
        lats.append(lat)
    for ring, _, _ in paths:
        for lon, lat in ring:
            lons.append(lon)
            lats.append(lat)

    if not lons:
        return None

    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)
    mid_lat = (min_lat + max_lat) / 2
    # Guarded: at the poles cos goes to zero and every longitude collapses to
    # one column, which divides by zero below.
    squeeze = max(math.cos(math.radians(mid_lat)), 1e-3)

    span_x = max((max_lon - min_lon) * squeeze, 1e-9)
    span_y = max(max_lat - min_lat, 1e-9)
    avail_w = PREVIEW_WIDTH - 2 * _PAD
    avail_h = PREVIEW_HEIGHT - 2 * _PAD
    scale = min(avail_w / span_x, avail_h / span_y)

    draw_w = span_x * scale
    draw_h = span_y * scale
    off_x = (PREVIEW_WIDTH - draw_w) / 2
    off_y = (PREVIEW_HEIGHT - draw_h) / 2

    def project(lon: float, lat: float) -> tuple[float, float]:
        x = off_x + (lon - min_lon) * squeeze * scale
        # SVG's y grows downward and latitude grows upward.
        y = off_y + (max_lat - lat) * scale
        return x, y

    return project


def _rgb(color: Sequence[int]) -> str:
    return f"rgb({int(color[0])},{int(color[1])},{int(color[2])})"


def _parquet_backed(record: LocalMap) -> bool:
    return any(
        isinstance(dataset, dict) and dataset.get("parquetUrl")
        for dataset in record.datasets
    )


# ---------------------------------------------------------------------------
# The page shell
# ---------------------------------------------------------------------------

#: `str.format` is used rather than an f-string because the stylesheet is full
#: of braces, and doubling every one of them is a page nobody can edit.
_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>kepler.gl maps</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 40px 32px 72px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    background: #0f1116; color: #e7ebf3;
    -webkit-font-smoothing: antialiased;
  }}
  header {{ max-width: 1400px; margin: 0 auto 28px; }}
  h1 {{ margin: 0 0 6px; font-size: 24px; font-weight: 650; letter-spacing: -0.01em; }}
  .sub {{ margin: 0; color: #8b95a7; font-size: 14px; overflow-wrap: anywhere; }}
  code {{
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
    font-size: 0.92em; color: #b9c2d2;
    background: #1a1e26; padding: 1px 5px; border-radius: 4px;
  }}
  .notice {{
    max-width: 1400px; margin: 16px auto 0; padding: 12px 16px;
    border: 1px solid #6b4a1f; background: #241c10; border-radius: 10px;
    color: #e8c89a; font-size: 14px;
  }}
  .grid {{
    max-width: 1400px; margin: 0 auto;
    display: grid; gap: 20px;
    /* `min()` so a narrow phone does not get a 320px column it cannot fit and
       a page that scrolls sideways. */
    grid-template-columns: repeat(auto-fill, minmax(min(320px, 100%), 1fr));
  }}
  .card {{
    background: #171a21; border: 1px solid #242a35; border-radius: 14px;
    overflow: hidden; display: flex; flex-direction: column;
    transition: border-color .15s ease, transform .15s ease;
  }}
  .card:hover {{ border-color: #3a4557; transform: translateY(-2px); }}
  .thumb {{ display: block; line-height: 0; background: #10131a; }}
  .preview {{ width: 100%; height: auto; display: block; }}
  .body {{ padding: 14px 16px 16px; display: flex; flex-direction: column; gap: 7px; flex: 1; }}
  h2 {{ margin: 0; font-size: 16px; font-weight: 620; line-height: 1.3; }}
  h2 a {{ color: #eef2f8; text-decoration: none; }}
  h2 a:hover {{ text-decoration: underline; }}
  .desc {{ margin: 0; font-size: 13px; color: #98a2b3; line-height: 1.45; }}
  .meta {{ margin: 0; font-size: 12px; color: #7b8598; }}
  .datasets {{ margin: 0; font-size: 12px; color: #616b7c; line-height: 1.4; }}
  .actions {{
    margin-top: auto; padding-top: 10px;
    display: flex; align-items: center; gap: 10px;
  }}
  .primary {{
    font-size: 13px; font-weight: 550; text-decoration: none; white-space: nowrap;
    color: #0f1116; background: #6fd0d8; padding: 6px 12px; border-radius: 7px;
  }}
  .primary:hover {{ background: #8adfe6; }}
  .quiet {{ font-size: 13px; color: #8b95a7; text-decoration: none; }}
  .quiet:hover {{ color: #c9d2e0; text-decoration: underline; }}
  .slug {{
    margin-left: auto; font-size: 11px; color: #5b6474;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    max-width: 45%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }}
  .empty {{
    max-width: 560px; margin: 48px auto; text-align: center; color: #8b95a7;
  }}
  .empty h2 {{ font-size: 18px; color: #dfe5ef; margin-bottom: 10px; }}
  .empty p {{ font-size: 14px; line-height: 1.6; margin: 8px 0; }}
  .empty .hint {{ font-size: 13px; color: #6b7688; }}
  @media (max-width: 520px) {{
    body {{ padding: 24px 16px 48px; }}
    .actions {{ flex-wrap: wrap; }}
    .slug {{ max-width: 100%; margin-left: 0; }}
  }}
</style>
</head>
<body>
<header>
  <h1>kepler.gl maps</h1>
  <p class="sub">{subtitle}</p>
</header>
{notice}
{body}
</body>
</html>
"""
