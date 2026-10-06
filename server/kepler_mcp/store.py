"""Maps on disk, without a database.

A local map is a directory:

    ~/kepler-maps/
      kepler-viewer.js           the bundle, 13 MB, written once
      kepler-viewer.version      its version stamp
      earthquakes/
        map.html                 the page, with its spec embedded
        spec.json                the same spec, for listing and for editing
        data/*.parquet           only when the rows are too big to inline

**A map is a directory, and the list is a scan.** The obvious alternative — an
index file naming every map — was rejected because an index is a second copy of
the truth that can disagree with it: a map deleted from Finder stays in the
index, a map copied in from a colleague is invisible. `list_maps` reads the
directories, so what is on disk and what is listed cannot drift.

**The bundle is written once, beside the maps, not inside each one.** It is
13 MB. Copying it per map would make "create a map" mean writing 13 MB, and
"keep twenty maps" mean 260 MB of identical JavaScript. Each `map.html`
references `../kepler-viewer.js`, which resolves both over `file://` and from
the preview server.

**The spec is written twice, on purpose, by one function.** `map.html` embeds
it because a `file://` page cannot fetch a sibling file — Chrome blocks it —
and a page that renders nothing is a worse artifact than a duplicated byte
range. `spec.json` holds it so the directory can be listed and re-opened
without parsing HTML. `write_map` is the only writer of either, so they cannot
disagree: the same in-memory spec produces both.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Filenames whose absence makes a directory not-a-map. `spec.json` alone: the
#: HTML can be regenerated from it, so it is the one that has to be there.
SPEC_NAME = "spec.json"
HTML_NAME = "map.html"
BUNDLE_NAME = "kepler-viewer.js"
BUNDLE_VERSION_NAME = "kepler-viewer.version"
DATA_DIR = "data"


class StoreError(RuntimeError):
    """A map could not be read or written, with a message meant for a human."""


def slugify(title: str, *, fallback: str = "map") -> str:
    """A directory name from a title.

    Kept deliberately conservative — lowercase ASCII, digits, hyphens — because
    this name becomes a path, and a path is the wrong place to discover that a
    title had a slash, a newline or a leading dot in it. Everything else is
    folded away, and the result is trimmed so it cannot become `.` or `..`.
    """
    # NFKD then drop combining marks, so "Süd" becomes "sud" rather than a
    # directory whose name depends on the filesystem's Unicode normalisation.
    decomposed = unicodedata.normalize("NFKD", title)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    slug = re.sub(r"-{2,}", "-", slug)
    # A slug of only dots or only dashes would resolve somewhere unintended.
    if not slug or slug.strip(".-") == "":
        return fallback
    return slug[:64]


@dataclass
class LocalMap:
    """A directory on disk holding one map."""

    slug: str
    directory: Path
    spec: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return self.spec.get("title") or self.slug

    @property
    def map_id(self) -> str:
        return self.spec.get("mapId") or self.slug

    @property
    def description(self) -> str | None:
        return self.spec.get("description")

    @property
    def datasets(self) -> list[dict]:
        return self.spec.get("datasets") or []

    @property
    def html_path(self) -> Path:
        return self.directory / HTML_NAME

    @property
    def spec_path(self) -> Path:
        return self.directory / SPEC_NAME

    @property
    def has_config(self) -> bool:
        """Whether a kepler config has been saved against this map.

        Distinguishes "created and never opened" from "opened and edited",
        which is what the listing reports and what decides whether the map is
        worth re-rendering.
        """
        return bool(self.spec.get("config"))

    def updated_at(self) -> datetime | None:
        """When the directory was last written.

        From the filesystem rather than from a field in the spec, because a
        spec edited by hand would otherwise carry a timestamp that is a lie, and
        a listing sorted by a lie is worse than one sorted by mtime.
        """
        try:
            return datetime.fromtimestamp(self.spec_path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None


class LocalStore:
    """The `~/kepler-maps` directory, as a collection of map directories."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- layout ------------------------------------------------------------

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def bundle_path(self) -> Path:
        return self.root / BUNDLE_NAME

    def ensure_bundle(self, source: Path, version: str | None) -> Path | None:
        """Place the viewer bundle beside the maps, if it is not already right.

        Compared by version stamp rather than by existence: an upgraded plugin
        must replace the bundle, or every map keeps rendering with the viewer
        it was created under and nothing about the page says so. Returns the
        path, or None when no bundle was found to copy.
        """
        if not source.exists():
            return None
        self.ensure()

        stamp_path = self.root / BUNDLE_VERSION_NAME
        existing = None
        try:
            existing = stamp_path.read_text().strip()
        except OSError:
            pass

        if existing == version and self.bundle_path.exists():
            return self.bundle_path

        # Copy to a temporary name and rename, so a map opened while the copy
        # is half-written cannot load a truncated bundle.
        tmp = self.bundle_path.with_suffix(".js.tmp")
        shutil.copyfile(source, tmp)
        tmp.replace(self.bundle_path)
        if version:
            stamp_path.write_text(version)
        return self.bundle_path

    def bundle_missing(self) -> bool:
        return not self.bundle_path.exists()

    # -- reading -----------------------------------------------------------

    def path_for(self, slug: str) -> Path:
        """The directory for a slug, refusing anything that escapes the root.

        `slugify` already produces safe names, but the slug can also arrive from
        a tool argument or a URL path, and `../../etc` is not a map. Resolving
        and comparing against the root is the check that actually holds.
        """
        candidate = (self.root / slug).resolve()
        root = self.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise StoreError(f"{slug!r} does not name a map under {self.root}")
        return candidate

    def get(self, slug: str) -> LocalMap:
        directory = self.path_for(slug)
        spec_path = directory / SPEC_NAME
        if not spec_path.exists():
            raise StoreError(
                f"No map called {slug!r} in {self.root}. Use `list_maps` to see "
                f"what is there."
            )
        try:
            spec = json.loads(spec_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreError(
                f"{spec_path} is not readable JSON — {exc}. The map's HTML may still "
                f"open; only the listing and re-editing need this file."
            ) from exc
        return LocalMap(slug=slug, directory=directory, spec=spec)

    def list_maps(self) -> list[LocalMap]:
        """Every subdirectory holding a `spec.json`, newest first.

        A directory without one is skipped rather than reported: `~/kepler-maps`
        is a plain directory the user also uses for other things, and a stray
        file there should not produce a broken entry.
        """
        if not self.root.exists():
            return []
        found: list[LocalMap] = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            try:
                found.append(self.get(entry.name))
            except StoreError:
                continue
        found.sort(key=lambda m: m.updated_at() or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return found

    def exists(self, slug: str) -> bool:
        try:
            return (self.path_for(slug) / SPEC_NAME).exists()
        except StoreError:
            return False

    def available_slug(self, title: str) -> str:
        """A slug based on the title that is not already taken.

        Suffixed rather than refused, because "make me a second map called
        Earthquakes" is a reasonable thing to want and a name collision is not
        the user's problem to solve.
        """
        base = slugify(title)
        if not self.exists(base):
            return base
        for n in range(2, 1000):
            candidate = f"{base}-{n}"
            if not self.exists(candidate):
                return candidate
        raise StoreError(f"Too many maps named {base!r} already.")

    # -- writing -----------------------------------------------------------

    def write_map(self, slug: str, spec: dict[str, Any]) -> LocalMap:
        """Write `map.html` and `spec.json` from one spec. The only writer.

        Both files come from the same dict in the same call, so they cannot
        drift; anything else that wants to change a map changes the spec and
        comes back through here.
        """
        from .maps import render_html  # local import: maps imports nothing here

        directory = self.path_for(slug)
        directory.mkdir(parents=True, exist_ok=True)

        html = render_html(spec, bundle_href=f"../{BUNDLE_NAME}")
        # tmp-then-rename for both, so a browser reload during a save reads
        # either the old file or the new one, never half of either.
        html_tmp = directory / f".{HTML_NAME}.tmp"
        html_tmp.write_text(html, encoding="utf-8")
        html_tmp.replace(directory / HTML_NAME)

        spec_tmp = directory / f".{SPEC_NAME}.tmp"
        spec_tmp.write_text(json.dumps(spec, indent=2), encoding="utf-8")
        spec_tmp.replace(directory / SPEC_NAME)

        return LocalMap(slug=slug, directory=directory, spec=spec)

    def write_data(self, slug: str, filename: str, payload: bytes) -> Path:
        """Put a Parquet file next to a map, at `data/<filename>`."""
        directory = self.path_for(slug) / DATA_DIR
        directory.mkdir(parents=True, exist_ok=True)
        # The filename arrives from a dataset name, so it gets the same escape
        # check the slug does.
        safe = Path(filename).name
        if not safe or safe.startswith("."):
            raise StoreError(f"{filename!r} is not a usable data filename")
        target = directory / safe
        target.write_bytes(payload)
        return target

    def delete(self, slug: str) -> Path:
        """Remove a map directory. Returns where it was.

        The caller is expected to have confirmed: this deletes data the user
        may have made and cannot be undone from here.
        """
        directory = self.path_for(slug)
        if not (directory / SPEC_NAME).exists():
            raise StoreError(f"No map called {slug!r} in {self.root}.")
        shutil.rmtree(directory)
        return directory

    def stats(self) -> str:
        """A one-line summary of the store, for `list_maps` and diagnostics."""
        maps = self.list_maps()
        if not maps:
            return f"No maps yet in {self.root}"
        total = sum(
            f.stat().st_size
            for m in maps
            for f in m.directory.rglob("*")
            if f.is_file()
        )
        return f"{len(maps)} map(s) in {self.root}, {_human_size(total)}"


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
