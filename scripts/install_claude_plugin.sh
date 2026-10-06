#!/bin/sh
# Install this checkout into Claude Code as a plugin.
#
# Claude Code installs a plugin by copying the marketplace's committed tree into
# `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` and running the
# server from *there*. That copy carries the launcher, the skill and the viewer
# bundle, but not the virtualenv — which is gitignored, built per machine, and
# never travels. So the launcher has to go looking for this checkout instead,
# and `scripts/record_plugin_root.sh` is what it finds.
#
# That recording is the step this script exists for. Install the plugin without
# it and everything looks right — `claude plugin list` shows it enabled — while
# every tool call fails with "no interpreter found". The error is legible, but
# it arrives at the first tool call rather than at install time, which is the
# worst place to learn it.
#
# Re-running is safe. A change to the server itself needs no reinstall at all:
# the launcher runs this checkout's own virtualenv, not a copy of it. Editing
# the skill does need one, because the skill is read from the cached copy.

set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)

# The CLI is not always on PATH — a fresh install on macOS puts it somewhere
# that only a login shell knows about. Override with CLAUDE_BIN.
if [ -n "${CLAUDE_BIN:-}" ]; then
    claude=$CLAUDE_BIN
elif command -v claude >/dev/null 2>&1; then
    claude=$(command -v claude)
else
    echo "install_claude_plugin: no claude CLI found; set CLAUDE_BIN" >&2
    exit 2
fi

# A plugin with no virtualenv installs cleanly and fails on first use, which is
# a worse failure to debug than one that refuses now. record_plugin_root.sh
# checks this too; it is repeated so the message names the actual problem.
if [ ! -x "$here/.venv/bin/kepler-gl-mcp" ]; then
    echo "install_claude_plugin: $here/.venv/bin/kepler-gl-mcp is missing." >&2
    echo "  Create it first:  uv venv && uv pip install -e ." >&2
    exit 1
fi

# Also checks that the viewer bundle is present, which is the other thing whose
# absence installs cleanly and shows up as a blank map.
"$here/scripts/record_plugin_root.sh"

echo "marketplace: $here"
"$claude" plugin marketplace add "$here" < /dev/null
"$claude" plugin install kepler.gl@kepler-gl < /dev/null

cat <<EOF

Installed. Check it with:

    $claude plugin list

The tools are named without a prefix in a session — \`create_map\`, \`list_maps\`
and the rest. Start a new session to pick the plugin up: one already open holds
the tools it started with.

    $here/.venv/bin/python scripts/smoke.py
EOF
