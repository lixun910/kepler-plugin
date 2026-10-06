#!/usr/bin/env python3
"""Bring the Codex plugin's own copy of things up to date, before installing it.

Two jobs, both about the fact that Codex runs a plugin from a *copy* it makes
under `~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/`:

1. **Copy the skill in.** Claude Code reads `skills/kepler-gl/` at the plugin
   root; Codex reads it from inside the plugin directory, and copies the plugin
   to get there. A symlink between the two would be tidier and does not survive
   that copy — the installed plugin ends up with no skill at all — so the tree
   is duplicated and `scripts/smoke.py` fails if the two ever differ.

2. **Move the version when the content moves.** `codex plugin add` is keyed on
   the version: install a plugin whose contents changed but whose version did
   not, and Codex reports success while continuing to run the old copy. Codex's
   own `update_plugin_cachebuster.py` solves this with a UTC timestamp in a
   `+codex.<token>` suffix; a hash of the plugin's contents is used here instead,
   which is the same shape and makes a re-run with nothing changed a no-op
   rather than a fresh version every time.

Run it directly, or via `scripts/install_codex_plugin.sh`, which also does the
marketplace plumbing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

#: The skill directory, relative to the repo root and to the plugin. Duplicated
#: rather than linked, for the reason in the module docstring.
SKILL = Path("skills") / "kepler-gl"

#: Files that must never be part of the content hash: the manifest, because the
#: version it holds is this calculation's own output and hashing it would never
#: converge. Everything else the plugin carries is hashed, including the viewer
#: bundle — a rebuilt viewer is a different plugin and Codex has to be told.
NOT_HASHED = {".codex-plugin/plugin.json"}


def content_hash(plugin: Path) -> str:
    """A hash of everything the plugin carries into the cache.

    Walked in sorted order so the digest does not depend on the filesystem's
    ordering, and over relative paths so moving the checkout does not change it.
    """
    digest = hashlib.sha256()
    for path in sorted(plugin.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(plugin).as_posix()
        if relative in NOT_HASHED or "__pycache__" in path.parts:
            continue
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def sync_skill(repo: Path, plugin: Path) -> bool:
    """Copy the skill tree into the plugin. True when anything changed."""
    source = repo / SKILL
    target = plugin / SKILL
    if not (source / "SKILL.md").is_file():
        raise FileNotFoundError(f"no skill at {source / 'SKILL.md'}")

    changed = False
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        destination = target / path.relative_to(source)
        payload = path.read_bytes()
        # Compared before writing, so a re-run touches nothing and the version
        # below does not move for a copy that changed no bytes.
        if destination.is_file() and destination.read_bytes() == payload:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        changed = True

    # A file removed from the source has to be removed here too, or the cached
    # plugin keeps serving a skill page the checkout no longer has.
    if target.is_dir():
        for path in sorted(target.rglob("*")):
            if path.is_file() and not (source / path.relative_to(target)).is_file():
                path.unlink()
                changed = True
    print(f"skill:     {source} -> {target}{' (changed)' if changed else ' (unchanged)'}")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "plugin",
        nargs="?",
        default=str(Path(__file__).resolve().parents[1] / "plugins" / "kepler.gl"),
        help="the Codex plugin directory",
    )
    args = parser.parse_args()
    plugin = Path(args.plugin).resolve()
    repo = Path(__file__).resolve().parents[1]

    sync_skill(repo, plugin)

    manifest_path = plugin / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text())
    # Everything before the cachebuster, so re-running replaces the suffix
    # rather than stacking a second one — `0.1.0+codex.a` -> `0.1.0+codex.b`,
    # never `0.1.0+codex.a+codex.b`. Codex's helper splits on "+" the same way.
    base = manifest["version"].split("+", 1)[0]
    version = f"{base}+codex.{content_hash(plugin)[:12]}"
    if version == manifest["version"]:
        print(f"version:   {version} (unchanged)")
        return 0
    manifest["version"] = version
    # `ensure_ascii=False` so the description keeps its em dashes rather than
    # being rewritten as `—` — the file is read by a person as often as by
    # Codex, and escaped punctuation is noise in a diff.
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"version:   -> {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
