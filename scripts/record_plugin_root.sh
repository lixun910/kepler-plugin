#!/bin/sh
# Record which checkout the plugin's MCP server should run from.
#
# Both clients install a plugin as a *copy* of its committed tree — Codex under
# ~/.codex/plugins/cache/, Claude Code under ~/.claude/plugins/cache/ — and the
# Python virtualenv the server lives in is gitignored, so it is built per
# machine and never travels into that copy. The launcher
# (plugins/kepler.gl/bin/kepler-mcp) searches for a checkout rather than being
# handed one, and this file is what a search from a cached copy can actually
# reach: walking up from ~/.claude/plugins/cache/… gets to $HOME and stops, and
# the checkout the plugin was installed from is a sibling of $HOME, not an
# ancestor.
#
# The viewer bundle *does* travel — it is committed, because a map has to render
# on a machine with no node_modules and no network — so this is only about
# finding the interpreter.
#
# Run it from a checkout that has been set up. Re-running is safe, and is what
# you do after moving the checkout somewhere else.

set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)

# A checkout with no virtualenv is not one the launcher can use, and recording
# it would trade a legible "no interpreter found" for a first tool call that
# fails for a reason nothing announces.
if [ ! -x "$here/.venv/bin/kepler-gl-mcp" ]; then
    echo "record_plugin_root: $here/.venv/bin/kepler-gl-mcp is missing." >&2
    echo "  Create it first:  uv venv && uv pip install -e ." >&2
    exit 1
fi

# A checkout without the viewer bundle installs cleanly and renders every map as
# a blank page, which is a worse failure than refusing now.
if [ ! -f "$here/plugins/kepler.gl/vendor/kepler-viewer.js" ]; then
    echo "record_plugin_root: the viewer bundle is missing from" >&2
    echo "  $here/plugins/kepler.gl/vendor/. Build it first:" >&2
    echo "      cd viewer && npm install && npm run build" >&2
    exit 1
fi

config_dir="${KEPLER_GL_CONFIG_DIR:-$HOME/.config/kepler-gl}"
mkdir -p "$config_dir"
printf '%s\n' "$here" >"$config_dir/plugin-root"
chmod 0644 "$config_dir/plugin-root"

echo "recorded $here in $config_dir/plugin-root"
