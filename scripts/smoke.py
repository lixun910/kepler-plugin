#!/usr/bin/env python3
"""End-to-end smoke test for the plugin's Python half.

Exercises the parts that are easy to get wrong and expensive to notice: the
dataset classification (which decides whether a map gets a layer at all), the
inline-versus-Parquet boundary, the page that gets written, the map index, and
the preview server's two non-obvious jobs — ranged reads, which is what makes a
Parquet-backed local map work, and the save token, which is what keeps a page in
some other tab from rewriting a map.

Run it from the repository root:

    .venv/bin/python scripts/smoke.py

Everything it writes goes to a temporary directory, so it is safe to run at any
time. It needs no account, no server and no network: nothing here touches the
hosted half, which is deliberate — the local half is the half that has to work
for a user who never signs in.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from kepler_mcp.config import Settings  # noqa: E402
from kepler_mcp.preview import stop_all  # noqa: E402
from kepler_mcp.tools import KeplerApp  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    if condition:
        print(f"  ok    {label}")
        return
    print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
    FAILURES.append(label)


def section(title: str) -> None:
    print(f"\n== {title}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def write_fixtures(directory: Path) -> dict[str, Path]:
    """Small files that cover each loader and each classification branch."""
    directory.mkdir(parents=True, exist_ok=True)
    made: dict[str, Path] = {}

    cities = directory / "cities.csv"
    cities.write_text(
        "name,latitude,longitude,population\n"
        + "\n".join(
            f"City {n},{35 + n * 0.4},{-120 + n * 0.7},{100_000 * n}"
            for n in range(1, 21)
        )
        + "\n"
    )
    made["cities"] = cities

    parks = directory / "parks.geojson"
    parks.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": f"Park {n}", "acres": n * 12.5},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-122.5 + n * 0.01, 37.7],
                                    [-122.4 + n * 0.01, 37.7],
                                    [-122.4 + n * 0.01, 37.8],
                                    [-122.5 + n * 0.01, 37.8],
                                    [-122.5 + n * 0.01, 37.7],
                                ]
                            ],
                        },
                    }
                    for n in range(1, 6)
                ],
            }
        )
    )
    made["parks"] = parks

    # A JSON array, which is the case that catches an all-VARCHAR table: its
    # coordinates have to be typed as numbers or no point layer can be built.
    readings = directory / "readings.json"
    readings.write_text(
        json.dumps(
            [
                {"station": f"S{n}", "lat": 37.7 + n * 0.01, "lng": -122.4, "value": n * 3}
                for n in range(1, 11)
            ]
        )
    )
    made["readings"] = readings

    # Past the inline cap, so it takes the Parquet path and the ranged reads.
    big = directory / "big.csv"
    with big.open("w") as handle:
        handle.write("id,h3,weight\n")
        for n in range(200_050):
            handle.write(f"{n},8a2a1072b59ffff,{n % 97}\n")
    made["big"] = big

    wkt = directory / "zones.csv"
    wkt.write_text(
        "zone,geometry\n"
        + "\n".join(
            f'Z{n},"POLYGON(({n} {n}, {n + 1} {n}, {n + 1} {n + 1}, {n} {n + 1}, {n} {n}))"'
            for n in range(1, 6)
        )
        + "\n"
    )
    made["zones"] = wkt

    return made


# ---------------------------------------------------------------------------


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="kepler-smoke-"))
    maps_dir = workdir / "maps"
    config_dir = workdir / "config"
    fixtures = write_fixtures(workdir / "fixtures")

    print(f"Working in {workdir}")

    settings = Settings(
        map_dir=maps_dir,
        config_dir=config_dir,
        redirect_uri="http://127.0.0.1:8976/callback",
    )
    settings.validate()
    app = KeplerApp(settings)

    # -- classification ----------------------------------------------------
    section("dataset classification")
    csv_report = app.inspect_data(str(fixtures["cities"]))
    check("kind **point**" in csv_report, "CSV lat/lng is classified as a point dataset")
    check(
        "Coordinates: `latitude`, `longitude`" in csv_report,
        "the coordinate columns are identified by name",
    )

    geojson_report = app.inspect_data(str(fixtures["parks"]))
    check(
        "kind **geojson**" in geojson_report and "_geojson" in geojson_report,
        "GeoJSON is classified as a geojson dataset with a _geojson column",
    )

    json_report = app.inspect_data(str(fixtures["readings"]))
    check(
        "kind **point**" in json_report,
        "a JSON array's coordinates keep their numeric type and classify as points",
        json_report[:400],
    )

    wkt_report = app.inspect_data(str(fixtures["zones"]))
    wkt_is_geojson = "kind **geojson**" in wkt_report
    wkt_needs_spatial = "spatial extension" in wkt_report
    check(
        wkt_is_geojson or wkt_needs_spatial,
        "a WKT column is either converted or refused for want of the extension",
        wkt_report[:400],
    )

    # -- map creation ------------------------------------------------------
    section("creating maps")
    report = app.create_map(
        [str(fixtures["cities"]), str(fixtures["parks"])],
        title="Smoke Test Map",
        description="fixtures",
    )
    check("Open: http://127.0.0.1:" in report, "create_map returns a loopback URL")
    check("File: " in report, "create_map returns the file path too")

    slug_dir = maps_dir / "smoke-test-map"
    check((slug_dir / "map.html").exists(), "map.html was written")
    check((slug_dir / "spec.json").exists(), "spec.json was written")
    check(
        (maps_dir / "kepler-viewer.js").exists(),
        "the viewer bundle was copied beside the maps",
    )

    spec = json.loads((slug_dir / "spec.json").read_text())
    check(spec["mapId"] == "smoke-test-map", "the map id is the directory name")
    check(
        spec["centreMap"] is True,
        "a fresh map fits its data rather than opening at the default viewport",
    )
    check(len(spec["datasets"]) == 2, "both datasets are in the spec")
    check(
        spec["save"]["url"] == "./__save",
        "the save target is relative, so the page works on any port",
    )
    check(
        "rows" in spec["datasets"][0],
        "small datasets are inlined, so the page works from file://",
    )
    vis = spec["config"]["config"]["visState"]
    check(len(vis["layers"]) == 2, "a layer was built for each dataset")
    check(
        {layer["type"] for layer in vis["layers"]} == {"point", "geojson"},
        "the layer types follow the dataset kinds",
    )
    tooltips = vis["interactionConfig"]["tooltip"]["fieldsToShow"]
    check(
        all(fields for fields in tooltips.values()) and len(tooltips) == 2,
        "tooltip fields are set for both datasets",
    )

    html = (slug_dir / "map.html").read_text()
    check("window.__KEPLER_MAP__" in html, "the spec is embedded in the page")
    check("../kepler-viewer.js" in html, "the page references the shared bundle")
    check("location.protocol !== 'file:'" in html, "the page adapts itself under file://")

    # An embedded `</script>` must not end the script block.
    nasty = workdir / "nasty.csv"
    nasty.write_text('label,latitude,longitude\n"</script><b>x</b>",1,2\n')
    app.create_map(str(nasty), title="Nasty Title")
    nasty_html = (maps_dir / "nasty-title" / "map.html").read_text()
    check(
        "</script><b>x</b>" not in nasty_html and "\\u003c/script>" in nasty_html,
        "a `</script>` inside the data is escaped rather than ending the block",
    )

    # -- the inline ceiling, and a big dataset ----------------------------
    section("the inline ceiling")
    big_report = app.create_map(str(fixtures["big"]), title="Big Map")
    big_spec = json.loads((maps_dir / "big-map" / "spec.json").read_text())
    big_dataset = big_spec["datasets"][0]
    check(
        "parquetUrl" in big_dataset,
        "the oversized dataset is referenced by URL, not inlined",
        json.dumps({k: v for k, v in big_dataset.items() if k != "rows"})[:200],
    )
    check("data/" in big_report and ".parquet" in big_report, "the report names the data file")
    parquet_name = big_dataset.get("parquetUrl", "").lstrip("./")
    parquet_path = maps_dir / "big-map" / parquet_name
    check(parquet_path.exists(), f"the Parquet file exists at {parquet_name}")
    check(
        big_report.count("Note:") >= 1,
        "the report says the data needs the preview URL rather than the file",
    )
    # The h3 column is what decides the layer here, so it is worth asserting
    # separately: it is the branch that has nothing to do with coordinates.
    check(big_dataset["id"].startswith("big"), "the dataset is named after its source")
    check(
        big_spec["config"]["config"]["visState"]["layers"][0]["type"] == "hexagonId",
        "a column of H3 cell ids builds a hexagonId layer",
        json.dumps(big_spec["config"]["config"]["visState"]["layers"][0])[:300],
    )

    # -- one map per directory, no overwriting ----------------------------
    app.create_map(str(fixtures["cities"]), title="Smoke Test Map")
    check(
        (maps_dir / "smoke-test-map-2").exists(),
        "a second map with the same title gets a suffix rather than overwriting",
    )

    # -- listing -----------------------------------------------------------
    section("listing")
    listing = app.list_maps()
    check("Smoke Test Map" in listing and "Big Map" in listing, "both maps are listed")
    check("smoke-test-map" in listing, "the listing names the directory to open")

    # -- the preview server ------------------------------------------------
    section("the preview server")
    open_report = app.open_map("big-map")
    url = next(
        (line.split("Open: ", 1)[1] for line in open_report.splitlines() if line.startswith("Open: ")),
        "",
    )
    check(url.startswith("http://127.0.0.1:"), "open_map returns a URL", open_report[:300])

    with httpx.Client(timeout=10.0) as client:
        page = client.get(url)
        check(page.status_code == 200, "the map page is served", str(page.status_code))
        check(
            "window.__KEPLER_MAP__" in page.text,
            "the served page carries the spec",
        )

        bundle = client.get(url.replace("/big-map/map.html", "/kepler-viewer.js"))
        check(bundle.status_code == 200, "the shared bundle is served")
        check(
            bundle.headers.get("content-type", "").startswith("text/javascript"),
            "the bundle is served as JavaScript",
            bundle.headers.get("content-type", ""),
        )

        parquet_url = url.replace("/big-map/map.html", f"/big-map/{parquet_name}")
        whole = client.get(parquet_url)
        check(whole.status_code == 200, "the Parquet file is served", str(whole.status_code))
        check(
            whole.headers.get("accept-ranges") == "bytes",
            "the Parquet response advertises range support",
        )

        # This is the request hyparquet actually makes, and the one a stock
        # SimpleHTTPRequestHandler answers with the whole file and a 200 — which
        # the reader then parses as though it were the footer.
        ranged = client.get(parquet_url, headers={"Range": "bytes=0-127"})
        check(
            ranged.status_code == 206,
            "a ranged request is answered with 206, not 200",
            str(ranged.status_code),
        )
        check(
            len(ranged.content) == 128,
            "the ranged response is exactly the bytes asked for",
            str(len(ranged.content)),
        )
        check(
            ranged.content == whole.content[:128],
            "the ranged bytes match the same slice of the whole file",
        )
        check(
            ranged.headers.get("content-range", "").startswith("bytes 0-127/"),
            "the ranged response states the byte range",
            ranged.headers.get("content-range", ""),
        )

        # hyparquet reads the footer with a suffix range before it knows the size.
        tail = client.get(parquet_url, headers={"Range": "bytes=-64"})
        check(
            tail.status_code == 206 and tail.content == whole.content[-64:],
            "a suffix range returns the last N bytes",
        )

        head = client.head(parquet_url)
        check(
            head.status_code == 200
            and head.headers.get("content-length") == str(len(whole.content)),
            "HEAD reports the length, which is how the reader sizes its reads",
            f"{head.status_code} {head.headers.get('content-length')} vs {len(whole.content)}",
        )

        # The bytes a browser would get have to be a real Parquet file, not a
        # JSON body with a Parquet content type.
        check(
            whole.content[:4] == b"PAR1" and whole.content[-4:] == b"PAR1",
            "the served file is Parquet, from its magic bytes",
            whole.content[:4].hex(),
        )

        # -- and the save endpoint ----------------------------------------
        section("saving from the page")
        token = json.loads((maps_dir / "big-map" / "spec.json").read_text())["save"]["headers"]
        new_config = {
            "version": "v1",
            "config": {
                "visState": {"layers": [], "filters": []},
                "mapState": {"latitude": 51.5, "longitude": -0.1, "zoom": 9},
                "mapStyle": {"styleType": "dark-matter"},
            },
        }
        refused = client.post(
            url.replace("/big-map/map.html", "/big-map/__save"),
            json={"mapId": "big", "config": new_config},
        )
        check(
            refused.status_code == 403,
            "a save without the token is refused",
            str(refused.status_code),
        )

        saved = client.post(
            url.replace("/big-map/map.html", "/big-map/__save"),
            json={"mapId": "big", "config": new_config},
            headers=token,
        )
        check(saved.status_code == 200, "a save with the token succeeds", saved.text[:200])

        after = json.loads((maps_dir / "big-map" / "spec.json").read_text())
        check(
            after["config"]["config"]["mapState"]["latitude"] == 51.5,
            "the saved config reached spec.json",
        )
        check(
            after["centreMap"] is False,
            "saving clears centreMap, so the viewport the user saved survives a reopen",
        )
        check(
            len(after["datasets"]) == 1 and after["title"] == "Big Map",
            "the save replaced only the config, leaving the datasets and title alone",
        )
        re_rendered = (maps_dir / "big-map" / "map.html").read_text()
        check(
            "51.5" in re_rendered,
            "map.html was re-rendered with the saved config",
        )

        missing = client.post(
            url.replace("/big-map/map.html", "/big-map/__save"),
            json={"mapId": "big"},
            headers=token,
        )
        check(missing.status_code == 400, "a body with no config is refused")

        # -- the map index -------------------------------------------------
        section("the map index")
        index_url = next(
            (
                line.split("with previews: ", 1)[1].strip()
                for line in app.list_maps().splitlines()
                if "with previews: " in line
            ),
            "",
        )
        check(
            index_url.startswith("http://127.0.0.1:"),
            "list_maps publishes the index URL",
            index_url,
        )

        index = client.get(index_url)
        check(index.status_code == 200, "the index is served", str(index.status_code))
        check(
            index.headers.get("content-type", "").startswith("text/html"),
            "the index is served as HTML",
            index.headers.get("content-type", ""),
        )
        check("<title>kepler.gl maps</title>" in index.text, "the index is the index page")

        expected = len(app.store.list_maps())
        check(
            index.text.count('class="card"') == expected,
            f"there is one card per map ({expected})",
            str(index.text.count('class="card"')),
        )
        # The thumbnail is drawn from the spec, so an inlined point dataset has
        # to produce circles and an inlined polygon dataset has to produce a
        # path — those are the two drawing branches, and a silent failure in
        # either is a card with an empty frame.
        check("<circle" in index.text, "an inlined point dataset is drawn")
        check("<polygon" in index.text, "an inlined polygon dataset is drawn")
        # `big-map` is Parquet-backed, so its rows are not in the spec and there
        # is nothing to draw; the card must say so rather than draw nothing.
        check(
            "held in data/*.parquet" in index.text,
            "a Parquet-backed map gets a labelled placeholder instead of a blank frame",
        )
        check(
            client.head(index_url).status_code == 200,
            "the index answers HEAD, like any other page it serves",
        )
        # Served at the root, but a browser that appends the filename must land
        # on the same page rather than on a 404.
        check(
            "kepler.gl maps" in client.get(index_url + "index.html").text,
            "index.html reaches the same page",
        )

    # A brand new maps directory has no cards to draw, and the page has to say
    # so rather than render an empty grid.
    from kepler_mcp.gallery import render_index  # noqa: E402

    check(
        "No maps yet" in render_index([], root=maps_dir),
        "an empty maps directory gets an empty state, not an empty grid",
    )

    # -- reopening ---------------------------------------------------------
    section("reopening")
    reopened = app.open_map("big-map")
    check("Open: http" in reopened, "a saved map reopens")
    check("51.5" in (maps_dir / "big-map" / "map.html").read_text(), "the saved viewport survives")

    # -- deletion ----------------------------------------------------------
    section("deletion")
    from kepler_mcp.tools import _guard  # noqa: E402

    # `confirm=False` with no client capabilities must refuse, not proceed: a
    # client that cannot draw the card must never be read as one that said yes.
    verdict = _run(app.delete_map("nasty-title", confirm=False))
    check(
        verdict.startswith("Not deleted."),
        "delete without confirmation refuses",
        verdict[:200],
    )
    check((maps_dir / "nasty-title").exists(), "the refused delete left the map alone")

    verdict = _run(app.delete_map("nasty-title", confirm=True))
    check(verdict.startswith("Deleted"), "delete with confirm=True proceeds", verdict[:200])
    check(not (maps_dir / "nasty-title").exists(), "the map directory is gone")

    # The index is rendered per request rather than written when a map changes,
    # so a directory removed from under it is gone on the next load with nothing
    # to regenerate. This is the check that the page did not become a file.
    with httpx.Client(timeout=10.0) as client:
        check(
            client.get(index_url).text.count('class="card"') == expected - 1,
            "a map deleted from the directory leaves the index on the next load",
        )

    # -- the MCP surface ---------------------------------------------------
    section("the MCP tool surface")
    from kepler_mcp.__main__ import build_server  # noqa: E402

    server = build_server(settings, app)
    names = sorted(server._tool_manager._tools) if hasattr(server, "_tool_manager") else []
    check(len(names) >= 15, "every tool registered", str(names))
    missing_docs = [
        name
        for name in names
        if not (server._tool_manager._tools[name].description or "").strip()
    ]
    check(not missing_docs, "every tool has a description", str(missing_docs))

    # A schema the client cannot parse is a tool the model cannot call, and the
    # union and dict annotations here are the ones that could produce one.
    import asyncio  # noqa: E402

    schemas = asyncio.run(server.list_tools())
    create_schema = next(
        (tool.input_schema for tool in schemas if tool.name == "create_map"), None
    )
    check(create_schema is not None, "create_map exposes an input schema")
    if create_schema:
        props = create_schema.get("properties", {})
        check("data" in props and "title" in props, "the schema names its arguments")
        check(
            "anyOf" in json.dumps(props.get("data", {})),
            "`data` accepts either a string or a list",
            json.dumps(props.get("data", {}))[:200],
        )

    # -- packaging ---------------------------------------------------------
    section("the plugin's packaging")
    plugin = ROOT / "plugins" / "kepler.gl"
    launcher = plugin / "bin" / "kepler-mcp"
    check(launcher.is_file(), "the MCP launcher is in the plugin")
    check(
        launcher.is_file() and os.access(launcher, os.X_OK),
        "the MCP launcher is executable — a client runs it directly",
    )
    check(
        (plugin / "vendor" / "kepler-viewer.js").is_file(),
        "the viewer bundle is committed, so a cached plugin can render a map",
    )
    check(
        (plugin / "vendor" / "kepler-viewer.version").is_file(),
        "the bundle carries its version stamp",
    )

    # Both clients read a JSON manifest, and a malformed one is a plugin that
    # installs and then does nothing — there is no error to read.
    for relative in (
        ".claude-plugin/plugin.json",
        ".claude-plugin/marketplace.json",
        ".agents/plugins/marketplace.json",
        ".mcp.json",
        "plugins/kepler.gl/.mcp.json",
        "plugins/kepler.gl/.codex-plugin/plugin.json",
    ):
        path = ROOT / relative
        try:
            json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            check(False, f"{relative} is valid JSON", str(exc))
        else:
            check(True, f"{relative} is valid JSON")

    root_mcp = json.loads((ROOT / ".mcp.json").read_text())
    command = (
        root_mcp.get("mcpServers", {}).get("kepler-gl", {}).get("command", "")
    )
    check(
        "${CLAUDE_PLUGIN_ROOT}" in command and command.endswith("/bin/kepler-mcp"),
        "the Claude manifest starts the launcher through the plugin root variable",
        command,
    )

    # Codex installs a *copy* of the plugin, and the skill has to be inside it:
    # the canonical copy at the repo root is what the author edits, and the two
    # drift silently if nothing compares them.
    canonical = ROOT / "skills" / "kepler-gl"
    carried = plugin / "skills" / "kepler-gl"
    check(
        (carried / "SKILL.md").is_file(),
        "the Codex plugin carries its own copy of the skill",
    )
    check(
        sorted(p.relative_to(canonical) for p in canonical.rglob("*") if p.is_file())
        == sorted(p.relative_to(carried) for p in carried.rglob("*") if p.is_file())
        and all(
            (canonical / p.relative_to(carried)).read_bytes() == p.read_bytes()
            for p in carried.rglob("*")
            if p.is_file()
        ),
        "the carried skill is identical to the canonical one — run "
        "scripts/sync_codex_plugin.py after editing either",
    )

    # The version has to move when the content does, or Codex reinstalls and
    # keeps running the copy it already had.
    manifest = json.loads((plugin / ".codex-plugin" / "plugin.json").read_text())
    check(
        "+codex." in manifest.get("version", ""),
        "the Codex manifest carries a content-derived cachebuster",
        manifest.get("version", ""),
    )

    # -- settings ----------------------------------------------------------
    section("settings")
    # The preview server's error text names KEPLER_GL_PREVIEW_PORT as the fix for
    # a port that cannot be bound, so the variable has to be read. A name that
    # appears only in a message is worse than no message.
    os.environ["KEPLER_GL_PREVIEW_PORT"] = "8931"
    try:
        pinned = Settings.load()
    finally:
        del os.environ["KEPLER_GL_PREVIEW_PORT"]
    check(pinned.preview_port == 8931, "the preview port is read from the environment")
    check(
        Settings().preview_port == 0,
        "and defaults to 0, so the OS picks a free port",
    )
    check(
        Settings(preview_port=70000).preview_port > 65535,
        "a port out of range is not silently clamped",
    )
    try:
        Settings(preview_port=70000).validate()
    except ValueError as exc:
        check("preview_port" in str(exc), "and is refused by validate()", str(exc))
    else:
        check(False, "and is refused by validate()", "no error raised")

    os.environ["KEPLER_GL_PREVIEW_PORT"] = "not-a-port"
    try:
        Settings.load()
    except ValueError as exc:
        check(
            "PREVIEW_PORT" in str(exc),
            "a non-numeric port is refused by name",
            str(exc),
        )
    else:
        check(False, "a non-numeric port is refused by name", "no error raised")
    finally:
        del os.environ["KEPLER_GL_PREVIEW_PORT"]

    stop_all()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All checks passed.")
    print(f"Artifacts left in {workdir}")
    return 0


def _run(coroutine):
    """Run a coroutine to completion, for the async tools called directly."""
    import asyncio

    return asyncio.run(coroutine)


if __name__ == "__main__":
    raise SystemExit(main())
