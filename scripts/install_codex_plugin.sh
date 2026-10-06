#!/bin/sh
# Install this checkout into Codex as a plugin.
#
# Codex installs plugins from a *marketplace*, not from an archive: a directory
# holding `.agents/plugins/marketplace.json` that lists plugins by relative
# path. This repo is that directory — `<repo-root>/.agents/plugins/marketplace.json`
# pointing at `<repo-root>/plugins/kepler.gl` — which is the layout Codex's
# own plugin-creator skill documents for a repo/team marketplace. So this script
# registers the checkout as a marketplace and installs the one plugin in it.
#
# Codex then copies the plugin to
# `~/.codex/plugins/cache/kepler-gl/kepler.gl/<version>/` and runs the server
# from *there*. That copy carries the skill, the launcher and the viewer bundle,
# but not the virtualenv, which is why the launcher has to go looking for it.
#
# Re-running is safe, and is how you pick up an edited skill: the skill copy and
# the version are refreshed first. A change to the server itself needs no
# reinstall at all — the launcher runs the checkout's own virtualenv.

set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)

# The CLI is not always on PATH. On macOS the ChatGPT app ships one inside its
# own bundle; anywhere else a normal `codex` is expected. Override with CODEX_BIN.
if [ -n "${CODEX_BIN:-}" ]; then
    codex=$CODEX_BIN
elif command -v codex >/dev/null 2>&1; then
    codex=$(command -v codex)
elif [ -x /Applications/ChatGPT.app/Contents/Resources/codex ]; then
    codex=/Applications/ChatGPT.app/Contents/Resources/codex
else
    echo "install_codex_plugin: no codex CLI found; set CODEX_BIN" >&2
    exit 2
fi

# A plugin with no virtualenv installs cleanly and fails on first use, which is
# a worse failure to debug than one that refuses now.
if [ ! -x "$here/.venv/bin/kepler-gl-mcp" ]; then
    echo "install_codex_plugin: $here/.venv/bin/kepler-gl-mcp is missing." >&2
    echo "  Create it first:  uv venv && uv pip install -e ." >&2
    exit 1
fi

python3 "$here/scripts/sync_codex_plugin.py"

# How the launcher finds the virtualenv from inside Codex's cached copy. The
# file holds a path, not a credential, and the launcher treats a missing one as
# "no candidate" rather than an error. Claude Code searches the same file from
# its own cache, so the recording is shared rather than repeated here.
"$here/scripts/record_plugin_root.sh"

echo "marketplace: $here"
"$codex" plugin marketplace add "$here" < /dev/null
"$codex" plugin add kepler.gl@kepler-gl < /dev/null

cat <<EOF

Installed. Check it with:

    $codex plugin list
    $codex mcp list

Start a new Codex thread to pick up the plugin — an open one holds the skills
and tools it started with.
EOF
