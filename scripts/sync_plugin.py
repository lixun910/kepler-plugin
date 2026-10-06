#!/usr/bin/env python3
"""Keep the committed plugin manifests in step with the plugin's content.

Both clients install a plugin by copying it into a cache keyed on the version,
and neither notices a content change under an unchanged version:

  * Codex copies to `~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/`,
    and `codex plugin add` reports success while continuing to run the copy it
    already had.
  * Claude Code copies to `~/.claude/plugins/cache/<marketplace>/<plugin>/
    <version>/`, and `claude plugin update` answers "already at the latest
    version" without re-reading the tree.

So each version carries a hash of the content it stands for, and shipping a
change is a content change plus a run of this script. Codex's own
`update_plugin_cachebuster.py` does the same job with a UTC timestamp in a
`+codex.<token>` suffix; a content hash is used here instead, which is the same
shape and makes a re-run with nothing changed a no-op rather than a fresh
version every time.

Three jobs.

1. **Copy the skill into the plugin.** Claude Code reads `skills/kepler-gl/` at
   the plugin root; Codex reads it from inside the plugin directory, and copies
   the plugin to get there. A symlink between the two would be tidier and does
   not survive that copy — the installed plugin ends up with no skill at all —
   so the tree is duplicated and `scripts/smoke.py` fails if the two ever differ.

2. **Stamp the Codex version**, from a hash of `plugins/kepler.gl` — the whole
   tree Codex copies.

3. **Stamp the Claude Code version**, from a hash of everything Claude's cache
   carries. That is a wider set than Codex's, and the difference is the reason
   this is one script rather than two: Claude's plugin root is the *repository*
   — `marketplace.json` points the plugin at `"./"` — so the skill and the
   subagent are read from the repo root, not from the copy inside
   `plugins/kepler.gl`. Hashing only the plugin directory would leave a skill
   edit that Codex picked up invisible to Claude.

The server is in neither hash, and does not need to be: the launcher execs the
checkout's own virtualenv, so a change under `server/` is live on the next tool
call with nothing to reinstall.

Run it directly, or through `scripts/install_claude_plugin.sh` or
`scripts/install_codex_plugin.sh`, which call it before installing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Sequence

#: The skill directory, relative to the repo root and to the plugin. Duplicated
#: rather than linked, for the reason in the module docstring.
SKILL = Path("skills") / "kepler-gl"

#: The plugin directory — what Codex copies wholesale, and what Claude Code
#: reaches the launcher, the skill copy and the viewer bundle through.
PLUGIN = Path("plugins") / "kepler.gl"

CODEX_MANIFEST = PLUGIN / ".codex-plugin" / "plugin.json"
CLAUDE_MANIFEST = Path(".claude-plugin") / "plugin.json"

#: What the Claude Code version stands for. Wider than the Codex set for the
#: reason in the module docstring: Claude's plugin root is the repository, so
#: `skills/` and `agents/` travel in its cache and are read from there.
CLAUDE_ROOTS = (
    PLUGIN,
    Path("skills"),
    Path("agents"),
    Path(".mcp.json"),
    Path(".claude-plugin"),
)

#: Never hashed: each holds a version this calculation produces, so hashing
#: either could never converge. Both are excluded from both hashes rather than
#: one each, because `plugins/kepler.gl` sits inside the Claude roots too.
MANIFESTS = frozenset({CODEX_MANIFEST, CLAUDE_MANIFEST})


def content_hash(repo: Path, roots: Sequence[Path]) -> str:
    """A hash of every file under `roots`, walked in sorted order.

    Sorted so the digest does not depend on the filesystem's ordering, and over
    repo-relative names so moving the checkout does not change it. A root may be
    a single file, which is how `.mcp.json` is covered.
    """
    entries: list[tuple[str, Path]] = []
    for root in roots:
        path = repo / root
        if path.is_file():
            entries.append((root.as_posix(), path))
        elif path.is_dir():
            entries.extend(
                (candidate.relative_to(repo).as_posix(), candidate)
                for candidate in sorted(path.rglob("*"))
                if candidate.is_file()
            )

    digest = hashlib.sha256()
    for relative, path in sorted(entries):
        if Path(relative) in MANIFESTS or "__pycache__" in path.parts:
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
    print(f"skill:          {source} -> {target}{' (changed)' if changed else ' (unchanged)'}")
    return changed


def stamp(manifest_path: Path, *, client: str, digest: str) -> bool:
    """Write one manifest's cachebuster. True when its version moved."""
    manifest = json.loads(manifest_path.read_text())
    # Everything before the cachebuster, so re-running replaces the suffix rather
    # than stacking a second one — `0.1.0+claude.a` becomes `0.1.0+claude.b`,
    # never `0.1.0+claude.a+claude.b`. Codex's helper splits on "+" the same way.
    base = str(manifest["version"]).split("+", 1)[0]
    version = f"{base}+{client}.{digest[:12]}"
    if version == manifest["version"]:
        print(f"{client} version:  {version} (unchanged)")
        return False
    manifest["version"] = version
    # `ensure_ascii=False` so a description keeps its em dashes rather than
    # being rewritten as `—` — the file is read by a person as often as by
    # a client, and escaped punctuation is noise in a diff.
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"{client} version:  -> {version}")
    return True


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    sync_skill(repo, repo / PLUGIN)
    stamp(repo / CODEX_MANIFEST, client="codex", digest=content_hash(repo, [PLUGIN]))
    stamp(repo / CLAUDE_MANIFEST, client="claude", digest=content_hash(repo, CLAUDE_ROOTS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
