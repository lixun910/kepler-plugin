/**
 * The redux store, and the two ways in and out of it.
 *
 * kepler.gl is a redux application: the map is a function of one state slice,
 * and everything the user does in the panel is a dispatched action against it.
 * That is what makes "change the map, then save it" possible at all — the
 * edited configuration is read back out of the store rather than scraped from
 * the DOM.
 *
 * `KeplerGlSchema.getConfigToSave` is the read side, and it is kepler's own
 * serializer rather than one written here. It knows which parts of `visState`
 * are configuration (layers, filters, interaction, animation) and which are
 * runtime (datasets, layerData, hover state) — a distinction that is invisible
 * until a config carrying runtime state is fed back in and produces a map with
 * duplicated or missing layers.
 */

import {addDataToMap, registerEntry} from '@kepler.gl/actions';
import {enhanceReduxMiddleware, keplerGlReducer} from '@kepler.gl/reducers';
// Named, for the same reason as `KeplerGl` in viewer.tsx: these packages are
// CommonJS, so a default import yields the namespace object and not the export.
import {KeplerGlSchema} from '@kepler.gl/schemas';
import {combineReducers, createStore, applyMiddleware, type Middleware, type Store} from 'redux';

import {loadDatasets} from './data';
import type {MapSpec} from './types';

/**
 * The thunk middleware, written out rather than imported.
 *
 * This is redux-thunk's entire implementation — twelve lines upstream, six
 * here — and it is here because importing it is a trap in this bundle.
 * `@reduxjs/toolkit`, which kepler pulls in, reaches redux-thunk through a
 * CommonJS `require`, so esbuild resolves that package to its CJS build for
 * every importer including this one. Under node-mode interop the resulting
 * `default` is the module namespace object rather than the middleware, so
 * `applyMiddleware` receives an object, and the store throws
 * "middleware is not a function" on the first dispatch rather than degrading:
 * the page renders its header over an empty map and nothing reaches the
 * console. Adding `redux-thunk@3` does not fix it either — the resolution is
 * by package, not by version.
 *
 * Every kepler action creator is a thunk, so this is load-bearing: without it
 * `addDataToMap` is dispatched as a plain object and the map never loads.
 */
const thunk: Middleware =
  ({dispatch, getState}) =>
  (next) =>
  (action) =>
    typeof action === 'function'
      ? (action as (d: unknown, g: unknown) => unknown)(dispatch, getState)
      : next(action);

/** The saved kepler config, as it is stored and re-loaded. */
export interface MapConfig {
  version: 'v1';
  config: {
    visState: Record<string, unknown>;
    mapState: Record<string, unknown>;
    mapStyle: Record<string, unknown>;
  };
}

export type KeplerStore = Store<{keplerGl: Record<string, unknown>}, never>;

/**
 * Build the store for one map, and register the map in it.
 *
 * The `registerEntry` dispatch is the load-bearing part, and it is not
 * optional. `keplerGlReducer` holds a *map* of instances keyed by map id, and
 * each action is routed to an instance only if that instance already exists —
 * "if you dispatch actions such as adding data to a kepler.gl instance before
 * the React component is mounted, the action will not be performed", in
 * kepler's own words. The `KeplerGl` component creates the instance from a
 * `useEffect` on mount, so without this the data load is a race against React:
 * it happens to win today because decoding a dataset yields to the event loop
 * first, and would silently lose the moment a dataset arrived synchronously.
 *
 * `mint: false` means "keep the instance state if this id is already
 * registered", which is what lets `<KeplerGl mint={false}>` mount afterwards
 * without minting a fresh, empty instance over the data loaded here.
 *
 * `readOnly` is settled at registration rather than by hiding controls
 * afterwards: kepler decides in the reducer whether the panel exists, so a
 * store built read-write and covered up still accepts edits from code. The
 * field is `readOnly`, not `readonly`, and the wrong spelling is accepted
 * without complaint — it merges into `uiState` as an ignored extra key and the
 * map stays editable.
 */
export function createKeplerStore(mapId: string, readOnly: boolean): KeplerStore {
  const reducers = combineReducers({keplerGl: keplerGlReducer});
  const middlewares = enhanceReduxMiddleware([thunk]);
  const store = createStore(reducers, {}, applyMiddleware(...middlewares)) as KeplerStore;

  store.dispatch(
    registerEntry({
      id: mapId,
      mint: false,
      initialUiState: readOnly ? {readOnly: true} : undefined
    }) as never
  );

  return store;
}

/** The map's own slice of the store — not the whole keplerGl reducer. */
function mapStateOf(store: KeplerStore, mapId: string): Record<string, unknown> | null {
  const keplerGl = store.getState().keplerGl as Record<string, unknown>;
  return (keplerGl?.[mapId] as Record<string, unknown>) ?? null;
}

/**
 * Read the current map back out as a savable config.
 *
 * Returns null before the first `addDataToMap` has landed: an empty store has
 * a slice, but saving it would write a config with no layers over whatever the
 * map was before.
 */
export function currentConfig(store: KeplerStore, mapId: string): MapConfig | null {
  const mapState = mapStateOf(store, mapId);
  if (!mapState || !mapState.visState) return null;
  return KeplerGlSchema.getConfigToSave(mapState) as unknown as MapConfig;
}

/**
 * Seed the store with the map's data and, when there is one, its saved config.
 *
 * The action is dispatched bare. kepler.gl v2 wrapped this in a
 * `wrapToDndContext` helper that supplied the drag-and-drop context for the
 * layer panel; v3 dropped that export and the `KeplerGl` component provides its
 * own context, so the wrapper is gone rather than merely optional.
 */
export async function loadMap(store: KeplerStore, spec: MapSpec): Promise<number> {
  const datasets = await loadDatasets(spec.datasets);
  const hasConfig = Boolean(
    spec.config && (spec.config as MapConfig)?.config?.visState
  );
  store.dispatch(
    addDataToMap({
      datasets: datasets as never,
      // `keepExistingConfig` is deliberately left alone. It defaults to false,
      // which is what makes a saved config replace the default one; setting it
      // true would silently ignore every layer the user saved.
      options: {
        centerMap: spec.centreMap !== false,
        // Only auto-create layers when there is no saved config. With one,
        // kepler's guesses would be added on top of the user's own layers.
        autoCreateLayers: !hasConfig,
        readOnly: Boolean(spec.readOnly)
      },
      config: hasConfig ? (spec.config as never) : undefined
    }) as never
  );
  return datasets.length;
}

/**
 * A stable string for "has this map changed since it was last saved".
 *
 * Compared by value rather than by reference because every dispatch produces a
 * new state object, and by serialization rather than by a deep equal because
 * the config is already JSON-shaped — it is going to be stringified on save
 * either way.
 *
 * Map key order is insertion order and therefore stable for a given kepler
 * version, so this does not produce false positives on an idle map.
 */
export function configFingerprint(config: MapConfig | null): string {
  if (!config) return '';
  try {
    return JSON.stringify(config);
  } catch {
    // A circular or BigInt-bearing state cannot be saved anyway. Returning a
    // unique value keeps the Save button enabled, so the failure surfaces on
    // the click the user made rather than by the button quietly staying off.
    return `unserialisable:${Date.now()}`;
  }
}

export {mapStateOf};
