# kepler.gl plugin

Build a kepler.gl map from a CSV, Parquet file, GeoJSON file or shapefile, get a
page you can open in a browser, edit and save — and, if you want, upload it to a
server that keeps the map config and the data as Parquet.

It works without an account. Signing in is what adds a place to put the maps.

The plugin is `kepler.gl`, so `@kepler.gl` reaches it in Claude Code, and Codex
sees the same tools through the plugin it installs.

> One consequence of that name: `claude plugin validate` warns that it is not
> kebab-case, which the Claude.ai marketplace sync requires. Local installs
> accept it either way. Renaming to `kepler-gl` would clear the warning and
> change what you type to reach the skill.

## What it does

**Local, with no account at all**

| Tool | |
| --- | --- |
| `inspect_data` | Read the schema of a file: columns, types, row count, which ones look like coordinates, which look like geometry. |
| `create_map` | Load a file, choose layers from what is actually in it, write a map directory, and serve it on a loopback URL. |
| `list_maps` | The maps on this machine — and the URL of the page that shows them all. |
| `open_map` | Serve one again — after a restart the URL has changed. |
| `delete_map` | Remove a map directory. Asks first. |

**On the server, once signed in**

| Tool | |
| --- | --- |
| `auth_status` · `login` · `logout` | Sign in through Auth0 in the browser. The token is cached, mode 0600, and refreshed until it is revoked. |
| `list_projects` · `create_project` | Projects group maps. |
| `upload_map` | Send a local map's config and data to a project. |
| `list_server_maps` · `open_server_map` | What is there, and a URL to look at it. |
| `update_server_map` | Push a map you edited locally back over the one on the server. |
| `delete_server_map` | Asks first. |

## Requirements

- Python 3.11 or newer
- Node 20 or newer, only if you are rebuilding the viewer bundle
- A kepler.gl **server** to sign in against — the `kepler-plugin-server` repo
  beside this one. The local half needs none of it, and neither does anything
  below the "Signing in" heading.

## Install

Two things have to exist on the machine before either client can start the MCP
server, and neither of them travels in the plugin.

```bash
uv venv && uv pip install -e .
```

```bash
scripts/record_plugin_root.sh
```

The first builds the Python package into a virtualenv beside the checkout. The
second records *where that checkout is*, and it is not optional: a client runs a
plugin from a copy it makes under `~/.claude/plugins/cache/` or
`~/.codex/plugins/cache/`, that copy never carries the virtualenv — it is
gitignored and built per machine — and walking up from the cache reaches `$HOME`
and stops. The checkout is a sibling of `$HOME`, not an ancestor, so the
recorded path is the only thing the launcher's search can actually find. Skip it
and everything looks installed while every tool call fails with "no interpreter
found". Re-run it after moving the checkout.

The viewer bundle is committed at `plugins/kepler.gl/vendor/kepler-viewer.js`, so
nothing else is needed to render a map. Rebuild it only after changing
`viewer/src/`:

```bash
cd viewer && npm install && npm run build
```

### Claude Code

```bash
scripts/install_claude_plugin.sh
```

That records the checkout and installs the plugin from this repo. By hand, from
the repo root, it is the two `claude plugin` commands below — but the recording
above is still what makes them work:

```bash
claude plugin marketplace add .
```

```bash
claude plugin install kepler.gl@kepler-gl
```

The published copy installs the same way and still needs a checkout on this
machine, because the server runs from here either way:

```bash
claude plugin marketplace add lixun910/kepler-plugin
```

Claude Code keys the install on the plugin's **version**, so
`scripts/sync_plugin.py` — which the installer runs — puts a hash of the plugin's
content in the version's build metadata. Without it, `claude plugin update`
answers "already at the latest version" at a copy that has since changed, which
is the same trap the Codex cachebuster exists for. `scripts/sync_plugin.py` is
the only writer of either manifest's version, so the two cannot name different
releases of the same plugin.

### Codex

Codex runs a plugin from a copy it makes under
`~/.codex/plugins/cache/`, which is why the skill is duplicated inside the
plugin, and why the plugin's version carries a hash of its contents — install a
changed plugin under an unchanged version and Codex keeps running the old copy.

```bash
scripts/install_codex_plugin.sh
```

That syncs the skill copy, moves the version, records the checkout, and runs the
two `codex plugin` commands. `scripts/sync_plugin.py` on its own does the skill
copy and moves both versions, which is what to run after editing anything under
`skills/kepler-gl/` — or under `agents/`, which Claude Code reads from the repo
root the same way.
`codex/config.toml.example` shows the equivalent configured by hand.

## Signing in

The identity provider is Auth0, in an application of type **Native**. Create
one, then register its callback:

```
http://127.0.0.1:8976/callback
```

The port is fixed and that is a constraint rather than a preference: Auth0
matches the callback against the registered string, and a registered string
carries its port. An ephemeral port would be a callback-mismatch page on every
login. Override it with `KEPLER_GL_REDIRECT_URI` and register the override.

Then set, in the environment or in `~/.config/kepler-gl/config.json`:

| Variable | |
| --- | --- |
| `KEPLER_GL_AUTH0_DOMAIN` | `your-tenant.us.auth0.com` |
| `KEPLER_GL_AUTH0_CLIENT_ID` | The native application's client id |
| `KEPLER_GL_AUTH0_AUDIENCE` | The API identifier the server validates |
| `KEPLER_GL_SERVER_URL` | Where the server is. `http://localhost:3000` by default. |

`KEPLER_GL_AUTH0_SCOPE` defaults to `openid profile email offline_access`; the
`offline_access` is what buys a refresh token, so a sign-in survives a restart
instead of prompting every session.

## How a map is stored

A map is a directory under `~/kepler-maps/`:

```
~/kepler-maps/
  kepler-viewer.js    the viewer bundle, shared by every map here
  my-map/
    spec.json         the kepler config — layers, viewport, basemap, tooltips
    map.html          the page: the spec inlined, the bundle referenced
    data/             one Parquet file per dataset, when too big to inline
```

`map.html` opens straight from `file://` — mail it, put it on a static host. The
spec is embedded in the page, so it carries its own layers and viewport, and the
bundle it loads is the one beside it. What a `file://` page cannot do is POST, so
the save target is dropped before the viewer starts rather than left as a button
whose every click fails; and it cannot fetch a sibling Parquet file, so a map too
large to inline opens read-only with those datasets left out and the reason
recorded. To edit a map, or to see one that is Parquet-backed, `open_map` serves
the directory over loopback instead.

Datasets under 200,000 rows and 48 MB are inlined into the page as JSON. Above
that they are written as Parquet and read by the page in ranges, which is why
the preview server answers `Range` requests: a 900 MB file opens without a
900 MB download.

Saving writes back to `spec.json` and re-renders `map.html`, and clears
`centerMap` so the viewport you saved is the one you get back — kepler's default
is to refit the view on load, which would throw away the zoom you chose.

## The map index

`list_maps` returns a table for an agent to read and the URL of a page for a
person to look at. That page is the index: every local map as a card, with its
title, description, datasets, layers, last-edited time and a thumbnail. It is
served from the same loopback port as the maps, at the server's root, and it
needs no account — like everything else on the local side.

The thumbnail is **drawn from the map's own spec** rather than framed: the
coordinates inlined in the spec, projected, in the colour of each layer. A live
iframe per card would be the obvious choice and the wrong one — each one loads
the 13 MB bundle and asks for its own WebGL context, and browsers stop handing
those out somewhere around sixteen, so a grid of twenty maps would half-render
with no error anywhere. A drawing costs nothing, needs no JavaScript, and shows
what a thumbnail is for: the shape of the data. A map whose rows live in
`data/*.parquet`, or one built on H3 cells or with no geometry at all, has no
coordinates in the spec to draw and gets a labelled placeholder instead — it
says which of those it is rather than showing an empty frame.

Nothing is written to disk for this. The page is rendered per request from a
fresh scan of the maps directory, for the reason `store.py` gives for scanning
rather than keeping an index file: a map deleted in Finder, or copied in from a
colleague, is right there on the next reload instead of being listed and gone.

## Layout

```
plugins/kepler.gl/     the plugin both clients install
  bin/kepler-mcp       the launcher — finds the interpreter, then execs it
  skills/kepler-gl/    its copy of the skill (Codex reads the copy)
  vendor/              the committed viewer bundle and its version stamp
server/kepler_mcp/     the MCP server
  tools.py             the 15 tools, and what each one refuses
  data.py              DuckDB: load, classify, decide inline vs Parquet
  layers.py            dataset kind -> kepler layer, with defaults that draw
  maps.py              the map directory, spec.json, map.html
  gallery.py           the map index page: a card and a drawing per map
  preview.py           the loopback server: ranged reads, the save token, the index
  auth.py              Auth0 PKCE, the loopback listener, the token cache
  store.py             local map bookkeeping
  api.py               the server's HTTP API, as the plugin calls it
skills/kepler-gl/      the canonical skill; the plugin's copy is generated
viewer/                the single-file kepler.gl viewer, built by esbuild
scripts/               install, sync, record-root, smoke test, index preview
```

## Developing

```bash
.venv/bin/python scripts/smoke.py
```

Runs the Python half end to end against temporary directories: dataset
classification, the inline/Parquet boundary, the page that gets written, ranged
reads, the save token, the tool surface, and the packaging — including that the
plugin's copy of the skill still matches `skills/kepler-gl/`, which is the one
thing here that can drift without anything failing.

Nothing in it touches the hosted half, on purpose: the local half is the half
that has to work for someone who never signs in.

There is a second check for the half that does, and it needs no credentials:

```bash
.venv/bin/python scripts/check_upload_contract.py
```

It stands a stub server in front of `ApiClient` and reads what the client
actually sends for an upload — the reserve call, the PUT to storage, the commit
— and what it does when each one fails. The upload is three calls because a
Vercel function cannot receive more than 4.5 MB, so the bytes never go through
the app; that makes it the one place where this plugin and
[`kepler-plugin-server`](../kepler-plugin-server) have to agree on a protocol
rather than on a URL, and a disagreement works against `next dev` and fails on
a real deployment.

The viewer has its own page for looking at a map in isolation:

```bash
cd viewer && npm run serve
```

The index page has one too. It builds a handful of sample maps covering every
branch the page draws — points, polygons, a Parquet-backed dataset with no
coordinates in its spec, a table with no geometry — in a temporary directory and
serves the index against them, so the page can be worked on without pointing it
at your own maps:

```bash
.venv/bin/python scripts/preview_index.py
```

## Configuration

Every variable is `KEPLER_GL_` prefixed, read from the environment and then from
`~/.config/kepler-gl/config.json` — the environment wins.

| | |
| --- | --- |
| `KEPLER_GL_CONFIG_DIR` | Where the token and config live. `~/.config/kepler-gl` |
| `KEPLER_GL_MAP_DIR` | Where maps are written. `~/kepler-maps` |
| `KEPLER_GL_SERVER_URL` | The hosted half |
| `KEPLER_GL_REDIRECT_URI` | The loopback callback. `http://127.0.0.1:8976/callback` |
| `KEPLER_GL_PREVIEW_PORT` | 0 — the OS picks — or a port to pin |
| `KEPLER_GL_VENDOR_DIR` | Where the viewer bundle is, if not next to the package |
| `KEPLER_GL_DEV_TOKEN` · `KEPLER_GL_DEV_MODE` | A pinned bearer token instead of signing in. Refused unless `DEV_MODE` is also set, so one left in the environment cannot quietly become the production path. |

## Licence

MIT
