/**
 * The bundle's entry point.
 *
 * Loaded as a classic script — not a module — because the pages it renders from
 * are `file://` documents as often as they are served ones, and a `file://` page
 * refuses to load an ES module (the request is cross-origin by definition). A
 * classic script from the same directory is allowed, which is what makes a map
 * written to disk work when double-clicked.
 *
 * Two ways in, and the automatic one is the normal one:
 *
 *   * **Automount.** A page that sets `window.__KEPLER_MAP__` before loading
 *     this file gets a map. This is what the plugin's HTML template and the
 *     hosted viewer both do, so neither has to know when this file has
 *     finished evaluating.
 *   * **`KeplerViewer.mount(spec)`.** For a page that wants to decide for
 *     itself, or to render a second map.
 */

import {mount} from './viewer';
// Declares `window.__KEPLER_MAP__` and `window.KeplerViewer`. Imported for that
// side effect as much as for the type: declaring the same global twice with
// different optionality is an error, and `types.ts` is where the contract with
// the host page belongs.
import './types';

//: Substituted at build time by build.mjs, from a hash of the sources it
//: bundled. A bare identifier rather than a string, because esbuild's `define`
//: rewrites identifiers and not the contents of string literals — as a string
//: it would ship the placeholder verbatim. It is what lets the Python side
//: notice that the bundle on disk came from a different revision.
declare const __KEPLER_VIEWER_VERSION__: string;
const VERSION = __KEPLER_VIEWER_VERSION__;

function automount(): void {
  const spec = window.__KEPLER_MAP__;
  if (spec) mount(spec);
}

window.KeplerViewer = {mount, version: VERSION};

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', automount);
} else {
  // The script was loaded at the end of the body, or injected after load.
  automount();
}

export {mount};
