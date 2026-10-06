#!/usr/bin/env python3
"""Serve the map index over a page of sample maps, for looking at it.

The index page is generated per request and normally lives behind whatever maps
the user happens to have made, which makes it awkward to work on: an empty
`~/kepler-maps` shows the empty state and nothing else. This builds a handful of
maps covering the branches the page has to draw — points, polygons, a layer that
colours by one of its columns, a Parquet-backed dataset with no coordinates in
the spec, a map with a description — in a temporary directory, and serves the
index against it.

    .venv/bin/python scripts/preview_index.py [--port 8933] [--keep]

Nothing here touches `~/kepler-maps` or the user's configuration: the maps go to
a temporary directory with its own config dir. The viewer bundle is read from
the checkout, so the map links on the cards work too.

`--keep` leaves the directory in place and prints its path, which is what to
reach for when the page looks wrong and the specs need reading.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from kepler_mcp.config import Settings  # noqa: E402
from kepler_mcp.preview import server_for, stop_all  # noqa: E402
from kepler_mcp.tools import KeplerApp  # noqa: E402


def _fixtures(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    made: dict[str, Path] = {}

    # Points spread over the western US, so the thumbnail has a shape.
    cities = directory / "cities.csv"
    cities.write_text(
        "name,latitude,longitude,population\n"
        + "\n".join(
            f"City {n},{32 + n * 0.35},{-124 + n * 0.42},{50_000 + n * 13_000}"
            for n in range(1, 60)
        )
        + "\n"
    )
    made["cities"] = cities

    # Polygons, which take the other drawing path in `gallery.py`.
    parks = directory / "parks.geojson"
    parks.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": f"Park {n}", "acres": n * 40.0},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-122.60 + n * 0.06, 37.72],
                                    [-122.52 + n * 0.06, 37.74],
                                    [-122.50 + n * 0.06, 37.83],
                                    [-122.58 + n * 0.06, 37.80],
                                    [-122.60 + n * 0.06, 37.72],
                                ]
                            ],
                        },
                    }
                    for n in range(1, 9)
                ],
            }
        )
    )
    made["parks"] = parks

    # Past the inline cap, so it takes the Parquet path and its rows are *not*
    # in the spec — the case that has to fall back to a placeholder thumbnail.
    big = directory / "readings.csv"
    with big.open("w") as handle:
        handle.write("id,h3,value\n")
        for n in range(200_050):
            handle.write(f"{n},8a2a1072b59ffff,{n % 89}\n")
    made["big"] = big

    # A table with no geometry at all: loads, gets no layer, draws nothing.
    bare = directory / "inventory.csv"
    bare.write_text(
        "sku,units,warehouse\n"
        + "\n".join(f"SKU-{n},{n * 3},W{n % 4}" for n in range(1, 25))
        + "\n"
    )
    made["bare"] = bare

    return made


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8933)
    parser.add_argument(
        "--keep", action="store_true", help="keep the temporary maps directory"
    )
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="kepler-index-"))
    fixtures = _fixtures(workdir / "fixtures")
    settings = Settings(
        map_dir=workdir / "maps",
        config_dir=workdir / "config",
        preview_port=args.port,
    )
    settings.validate()
    app = KeplerApp(settings)

    samples = (
        ("Western cities", ["cities"], "Population across the west.", {}),
        (
            "Cities by population",
            ["cities"],
            "Coloured by the population column.",
            {"*": {"color_field": "population"}},
        ),
        ("Bay Area parks", ["parks"], "", {}),
        ("Cities and parks", ["cities", "parks"], "Two datasets, two layers.", {}),
        ("H3 readings", ["big"], "Too big to inline — Parquet-backed.", {}),
        ("Warehouse inventory", ["bare"], "No geometry, so no layer.", {}),
    )
    for title, names, description, options in samples:
        report = app.create_map(
            [str(fixtures[name]) for name in names],
            title=title,
            description=description,
            options=options,
        )
        # Flushed, because the URL below is the whole point of running this and
        # a piped stdout would otherwise hold it until the process exits.
        print(report.splitlines()[0], flush=True)

    url = server_for(app.store, port=args.port).index_url
    print(f"\nMap index: {url}")
    print(f"Maps in:   {settings.maps_dir}")
    print("Ctrl-C to stop.\n", flush=True)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        stop_all()
        if args.keep:
            print(f"Kept {workdir}")
        else:
            import shutil

            shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
