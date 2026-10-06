"""The map page: the spec the viewer reads and the HTML that carries it.

Two functions matter here. `build_spec` produces the `MapSpec` described in the
viewer's `types.ts` — the contract between this side and the bundle — and
`render_html` wraps one in a page.

**The page is generated, not templated and filled.** There is one `MapSpec` dict
and the HTML is a function of it, so there is nothing to keep in step: the spec
is what the viewer reads, what `spec.json` stores, and what the Save handler
writes back.

**Embedding JSON in a script tag is a quoting problem with teeth.** A dataset
holding the string `</script>` — an address, a place name, a column of scraped
HTML — ends the script block early and the rest of the JSON lands in the DOM as
text. `str.replace("<", "\\u003c")` after `json.dumps` is the fix and it is
complete: `<` is the only character that can begin a sequence a browser will act
on inside a script element, and escaping it keeps the JSON valid because `<`
is the same character. Escaping the slash instead is the common half-measure and
misses `<!--`, which is a comment opener in a classic script.

**A page whose bundle failed to load is a blank page with no explanation.** The
bundle is a separate file, referenced by a relative path, and a map directory
copied somewhere without it renders as nothing at all. The check at the end of
the page costs three lines and turns that into a sentence naming the file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .data import LoadedTable
from .layers import build_config

#: Where a hosted map's Save button posts, relative to the page. The app
#: serves the page from the map's own URL, so a relative path is right and an
#: absolute one would have to know the deployment's hostname.
SAVE_PATH = "./save"


def label_for(source: str) -> str:
    """A readable label from a file path or URL.

    `2024-earthquakes_m5.csv` becomes `2024 earthquakes m5`. Worth the fourteen
    lines because this string is what the layer is called in the legend, and a
    legend reading `2024_earthquakes_m5_csv` is a legend the user has to decode.

    **A generic filename is replaced by its directory's name**, and that is the
    case that matters in practice: a download or an extract is almost always
    `<something>/data.csv`, so `earthquakes/data.csv` would otherwise produce a
    dataset, a layer, a legend entry, a Parquet file and a tooltip key all
    called `data_1`. The directory is the part of the path that carries the
    meaning; the filename is boilerplate.
    """
    cleaned_path = source.split("?")[0].rstrip("/")
    path = Path(cleaned_path)
    name = path.name
    for suffix in (".geojson", ".ndjson", ".jsonl", ".parquet", ".json", ".csv", ".tsv", ".shp", ".gpkg"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break

    stem = _readable(name)
    if stem.lower() in _GENERIC_NAMES:
        parent = _readable(path.parent.name)
        # A bare `data.csv` in the current directory has no parent to borrow
        # from — `Path("data.csv").parent.name` is `""`.
        if parent:
            return parent
    return stem or "dataset"


#: Filenames that say nothing about their contents. A download, an export from
#: a spreadsheet, a file extracted from a zip: the name is a convention of the
#: tool that produced it, not a description of the data.
_GENERIC_NAMES = frozenset(
    {
        "data",
        "dataset",
        "datasets",
        "export",
        "exports",
        "output",
        "out",
        "file",
        "table",
        "sheet",
        "sheet1",
        "untitled",
        "sample",
        "tmp",
        "temp",
    }
)


def _readable(name: str) -> str:
    return " ".join(name.replace("_", " ").replace("-", " ").split())


def dataset_spec(
    loaded: LoadedTable,
    *,
    rows: list[dict[str, Any]] | None = None,
    parquet_url: str | None = None,
    dataset_id: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """One entry of `MapSpec.datasets`.

    Exactly one of `rows` and `parquet_url` must be given, and the choice is
    made by the caller because only it knows the size of the data and whether
    the page will be opened from the filesystem or from a server. See
    `data.INLINE_ROW_CAP`.
    """
    if (rows is None) == (parquet_url is None):
        raise ValueError("dataset_spec needs exactly one of rows or parquet_url")

    spec: dict[str, Any] = {
        # The table name, not the label. kepler keys every layer's `dataId` by
        # this, so it has to be the same string here, in the layer config, and
        # in the tooltip field map — while the label is free to be pretty.
        "id": loaded.table,
        "label": label or label_for(loaded.source),
        "kind": loaded.kind,
    }
    if rows is not None:
        spec["rows"] = rows
    else:
        spec["parquetUrl"] = parquet_url
    if dataset_id:
        # Recorded for the plugin's own use — it is what a later
        # `update_map` needs to re-point at the same stored object rather than
        # uploading the file again.
        spec["datasetId"] = dataset_id
    return spec


def build_spec(
    *,
    map_id: str,
    title: str,
    datasets: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
    description: str | None = None,
    read_only: bool = False,
    centre_map: bool = True,
    theme: str = "dark",
    save: dict[str, Any] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """The `MapSpec` the bundle loads.

    `centre_map` is passed straight to kepler's `centerMap`, which refits the
    viewport to the data on load. The caller decides it, and the rule is about
    *provenance*, not about whether a config is present:

      * A map being built for the first time should fit its data. Its `mapState`
        is a placeholder — nobody has looked at it — so leaving `centreMap` off
        opens every map at the default viewport, with the data somewhere off the
        edge of the screen. That is a map that looks broken.
      * A map whose viewport has been *edited* must not be refitted, because the
        zoom and pan are part of what the user saved. That is why the preview
        server's save handler clears this flag as it writes: after the first
        save there is a real viewport to respect.

    Deriving it from `config is not None` was the earlier rule and it was wrong
    in exactly the case that matters — the config this plugin just generated.
    """
    spec: dict[str, Any] = {
        "mapId": map_id,
        "title": title,
        "datasets": datasets,
        "readOnly": bool(read_only),
        "centreMap": bool(centre_map),
        "theme": "light" if theme == "light" else "dark",
    }
    if description:
        spec["description"] = description
    if config:
        spec["config"] = config
    if save:
        spec["save"] = save
    if notes:
        spec["notes"] = notes
    return spec


def config_for(
    tables: list[LoadedTable],
    *,
    layers: list[dict[str, Any]] | None = None,
    map_state: dict[str, Any] | None = None,
    style: str = "dark",
) -> dict[str, Any]:
    """A config for a fresh map from loaded tables and their layers."""
    return build_config(
        layers=layers or [], tables=tables, map_state=map_state, style=style
    )


def render_html(spec: dict[str, Any], *, bundle_href: str, extra_head: str = "") -> str:
    """The whole page, with the spec embedded and the bundle referenced.

    `bundle_href` is relative: `../kepler-viewer.js` for a local map, whose
    directory sits beside the shared bundle. The hosted app passes its own path
    and this function does not care which — a page rendered by the server and a
    page rendered here differ in that attribute and in `save`, and nowhere else,
    which is what makes "the same map looks the same in both places" true rather
    than aspirational.
    """
    title = escape_html(spec.get("title") or "Kepler.gl map")
    payload = json.dumps(spec, ensure_ascii=False, separators=(",", ":"))
    # See the module docstring: `<` is the character that ends a script block,
    # and escaping it keeps the JSON valid.
    payload = payload.replace("<", "\\u003c")
    theme = spec.get("theme") or "dark"
    background = "#12141a" if theme != "light" else "#f4f5f7"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  html, body {{ margin: 0; height: 100%; background: {background}; }}
  #kepler-root {{ height: 100%; }}
  .kv-boot {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    color: #858e9d; padding: 48px; max-width: 640px; margin: 0 auto; line-height: 1.6;
  }}
  .kv-boot code {{ color: #d5dae3; }}
</style>
{extra_head}</head>
<body>
<div id="kepler-root"><div class="kv-boot">Loading the map…</div></div>
<script>window.__KEPLER_MAP__ = {payload};</script>
<script>
// A map directory is opened two ways: from the filesystem, and through the
// preview server. The spec has to be true in both, and the difference is the
// protocol. Rather than render two pages and let them drift, one page adjusts
// itself — and it does so *before* the bundle loads, so the viewer never sees
// a save target it cannot use.
(function () {{
  if (location.protocol !== 'file:') return;
  var spec = window.__KEPLER_MAP__ || {{}};
  // A `file://` page cannot POST. Leaving the target in place would give a
  // Save button whose every click fails, which is worse than no button.
  delete spec.save;
  // Nor can it fetch a sibling Parquet file: Chrome treats a file page's
  // origin as opaque and blocks the request. Those datasets are dropped with
  // the reason recorded, so the rest of the map still draws.
  var dropped = 0;
  if (Array.isArray(spec.datasets)) {{
    spec.datasets = spec.datasets.filter(function (d) {{
      if (!d || !d.parquetUrl) return true;
      dropped += 1;
      return false;
    }});
  }}
  if (dropped) {{
    // The header's own status line already says "not connected to a server"
    // when there is no save target; only the dropped data needs saying, because
    // nothing else on the page would mention it.
    spec.notes = (spec.notes || []).concat([
      dropped + ' dataset(s) held in data/*.parquet did not load — open this map ' +
      'through the preview server to see them'
    ]);
  }}
}})();
</script>
<script src="{escape_html(bundle_href)}"></script>
<script>
// The bundle is a separate file. When it is missing — the map directory was
// copied without it, or the plugin was never built — the page is otherwise
// blank, and a blank page is indistinguishable from a map with no data.
(function () {{
  if (window.KeplerViewer) return;
  var root = document.getElementById('kepler-root');
  if (!root) return;
  root.innerHTML =
    '<div class="kv-boot"><strong>The map could not be loaded.</strong><br>' +
    'The viewer bundle <code>{escape_html(bundle_href)}</code> did not load. ' +
    'Check that the file exists next to this map.</div>';
}})();
</script>
</body>
</html>
"""


def escape_html(text: str) -> str:
    """Escape for an HTML text or attribute position.

    Quotes are included because the same helper fills attributes: a title
    containing a double quote would otherwise end the attribute and start
    something else.

    Public because `gallery.py` fills attributes and text with the same
    strings — map titles and descriptions come from the user — and a second
    copy of this is a second thing to get subtly wrong.
    """
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )
