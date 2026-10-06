/**
 * Build the two copies the test pages load, so a bug can be read in a stack
 * trace instead of guessed at from a minified column number.
 *
 * `test/kepler-viewer.debug.js` is the same bundle unminified. It is 28 MB, it
 * is never shipped, and it exists for exactly one reason: when the minified
 * build throws `TypeError: l is not a function`, the unminified one says
 * `middleware is not a function at createKeplerStore`. Both bugs found while
 * wiring up the store were located that way.
 *
 * `test/kepler-viewer.js` is the real, minified build, copied here so the two
 * pages differ only in that one flag.
 *
 * Run `npm run build` first — this does not build the shipped bundle.
 */

import {build} from 'esbuild';
import {copyFile, stat} from 'node:fs/promises';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const SHIPPED = join(here, '..', 'plugins', 'kepler.gl', 'vendor', 'kepler-viewer.js');
const TEST_MIN = join(here, 'test', 'kepler-viewer.js');
const TEST_DEBUG = join(here, 'test', 'kepler-viewer.debug.js');

/** Shared by both builds — only `minify` differs. Kept in step with build.mjs. */
const common = {
  entryPoints: [join(here, 'src', 'index.tsx')],
  bundle: true,
  format: 'iife',
  platform: 'browser',
  target: ['es2020'],
  sourcemap: false,
  legalComments: 'none',
  banner: {
    js: 'var process=typeof process!=="undefined"?process:{env:{NODE_ENV:"production"}};'
  },
  define: {
    __KEPLER_VIEWER_VERSION__: JSON.stringify('debug'),
    'process.env.NODE_ENV': '"production"'
  },
  loader: {'.tsx': 'tsx', '.ts': 'ts'},
  splitting: false,
  logLevel: 'warning'
};

const mb = (bytes) => `${(bytes / 1024 / 1024).toFixed(2)} MB`;

await build({...common, outfile: TEST_DEBUG, minify: false});
await copyFile(SHIPPED, TEST_MIN);

const debug = (await stat(TEST_DEBUG)).size;
console.log(`test/kepler-viewer.debug.js  ${mb(debug)}  (unminified)`);
console.log('test/kepler-viewer.js        (copy of the shipped bundle)');
console.log('\nopen http://localhost:8919/debug.html  — errors land in window.__errs');
