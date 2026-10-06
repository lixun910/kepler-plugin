/**
 * Build `kepler-viewer.js` — one classic script holding kepler.gl, React, the
 * Parquet reader and the save button.
 *
 * Three constraints decide the shape of this file, and each of them is a bug
 * that was paid for once already:
 *
 *   * **IIFE, not ESM.** The output is loaded by `file://` pages. A module
 *     script is fetched as a cross-origin request from a `file://` document and
 *     is refused; a classic script in the same directory is not.
 *
 *   * **One file, no code splitting.** The bundle is written next to the map
 *     HTML and referenced by a relative path. A chunk graph would be a
 *     directory of files to keep together, and the page has no server to
 *     resolve a relative import against.
 *
 *   * **`process.env.NODE_ENV` is defined, not read.** React and styled-
 *     components branch on it at module scope. Left undefined, the bundle falls
 *     into the development branch: several hundred kilobytes larger, and
 *     noticeably slower to render a map. Defining that one key is not enough on
 *     its own, which is what the banner below is for: some dependency reaches
 *     for `process` itself — `process.version`, an env probe — and a bare
 *     reference to an undeclared global is a `ReferenceError` at the top of a
 *     classic script, so the whole bundle dies before `KeplerViewer` exists and
 *     the page renders empty with nothing in the console to say why.
 *
 * Minification is on, but identifiers are not mangled to the point of breaking
 * styled-components' display names — kepler styles components with template
 * literals that carry their own names, so `keepNames` is only needed for the
 * error messages. It is left off for size; the names in the trace are not worth
 * the 400 KB.
 */

import {build} from 'esbuild';
import {createHash} from 'node:crypto';
import {readFile, writeFile, mkdir} from 'node:fs/promises';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, '..');

/** Where the plugin ships the bundle from. Committed, so an install is enough. */
const OUT = join(repo, 'plugins', 'kepler.gl', 'vendor', 'kepler-viewer.js');

/** A second copy the hosted app serves as a static asset. */
const SERVER_OUT = join(repo, '..', 'kepler-plugin-server', 'public', 'vendor', 'kepler-viewer.js');

/**
 * A version string the Python side can check the bundle against.
 *
 * Derived from the source rather than from a hand-edited constant: the failure
 * this prevents is a bundle left over from an earlier checkout — the plugin
 * would render maps with whatever the viewer used to do, and nothing would say
 * so. It is substituted into index.tsx at build time so the page and the file
 * agree by construction.
 */
async function sourceVersion() {
  const hash = createHash('sha256');
  for (const file of ['src/index.tsx', 'src/viewer.tsx', 'src/store.ts', 'src/data.ts']) {
    hash.update(await readFile(join(here, file)));
  }
  return hash.digest('hex').slice(0, 12);
}

async function buildOnce(version) {
  const result = await build({
    entryPoints: [join(here, 'src', 'index.tsx')],
    bundle: true,
    outfile: OUT,
    format: 'iife',
    platform: 'browser',
    target: ['es2020'],
    minify: true,
    sourcemap: false,
    legalComments: 'none',
    // Runs before the bundle body, and outside its IIFE, so `process` is a
    // global by the time anything asks for it. `typeof` guards the real one:
    // under a bundler that does define it, this is a no-op rather than a
    // shadow.
    banner: {
      js: 'var process=typeof process!=="undefined"?process:{env:{NODE_ENV:"production"}};'
    },
    // The substitution the version check depends on.
    define: {
      __KEPLER_VIEWER_VERSION__: JSON.stringify(version),
      'process.env.NODE_ENV': '"production"'
    },
    loader: {'.tsx': 'tsx', '.ts': 'ts'},
    // A `file://` page has no origin to make a CORS request from, so nothing
    // may be fetched at runtime that was not bundled. Everything is inlined;
    // this fails the build rather than emitting a chunk if that ever changes.
    splitting: false,
    metafile: true,
    logLevel: 'info'
  });

  const bytes = Object.values(result.metafile.outputs)[0]?.bytes ?? 0;
  return bytes;
}

async function main() {
  const version = await sourceVersion();
  const bytes = await buildOnce(version);
  await mkdir(dirname(OUT), {recursive: true});

  // The version is baked in by `define`, so re-read the file to confirm the
  // substitution actually happened — a typo in the identifier would otherwise
  // ship a literal placeholder that silently never matches.
  const built = await readFile(OUT, 'utf8');
  if (built.includes('__KEPLER_VIEWER_VERSION__')) {
    throw new Error('version placeholder survived the build');
  }
  await writeFile(join(dirname(OUT), 'kepler-viewer.version'), `${version}\n`);

  console.log(
    `kepler-viewer.js  ${(bytes / 1024 / 1024).toFixed(2)} MB  version ${version}`
  );

  // The server copy is best-effort: the plugin repo builds without its
  // neighbour checked out, and a served copy is not required for a local map.
  try {
    await mkdir(dirname(SERVER_OUT), {recursive: true});
    await writeFile(SERVER_OUT, built);
    console.log(`   -> ${SERVER_OUT}`);
  } catch {
    console.log('   (no kepler-plugin-server checkout next to this one; skipped)');
  }
}

await main();
