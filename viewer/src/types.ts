/**
 * The contract between a rendered map page and this bundle.
 *
 * The page (written by the plugin's Python server, or by the hosted app) sets
 * `window.__KEPLER_MAP__` to one of these and then loads the bundle, which
 * mounts itself. Nothing else is required of the host: the bundle owns the
 * store, the map, and the save button.
 *
 * The same bundle renders both kinds of page, which is the point — a map
 * opened from a laptop and the same map opened from the server are drawn by
 * identical code, so a config that looks right in one cannot look wrong in the
 * other.
 */

/** How a dataset's rows reach the browser. */
export type DatasetSource =
  /** Rows are on the page. The plugin uses this below its inline row cap. */
  | {rows: Array<Record<string, unknown>>}
  /** Rows are a Parquet object, fetched and decoded with hyparquet. */
  | {parquetUrl: string};

/**
 * What a dataset holds, which decides how kepler is handed it.
 *
 * `point` and `table` become row datasets; `geojson` becomes a
 * FeatureCollection. The Python side decides this when the data is loaded and
 * records it in the dataset's metadata, so it never has to be guessed here
 * from the column names.
 */
export type DatasetKind = 'point' | 'geojson' | 'h3' | 'table';

/**
 * An intersection rather than an interface, because `DatasetSource` is a union
 * — `interface X extends A | B` does not compile, and the failure reads as a
 * complaint about the union rather than about the `extends`.
 */
export type DatasetSpec = DatasetSource & {
  id: string;
  label: string;
  kind: DatasetKind;
};

/** Where the Save button posts, and what it sends. */
export interface SaveTarget {
  /** Absolute or relative URL the current config is POSTed to. */
  url: string;
  /** Headers beyond `Content-Type`. */
  headers?: Record<string, string>;
  /**
   * What the button says while it is idle. A local map saves to its own
   * loopback server, a hosted one to the app — the wording is the host's to
   * choose because only it knows which.
   */
  label?: string;
}

export interface MapSpec {
  /** kepler's state key, and the id echoed back on save. */
  mapId: string;
  title?: string;
  description?: string;
  datasets: DatasetSpec[];
  /** A saved kepler config (`{version, config:{visState, mapState, mapStyle}}`). */
  config?: unknown | null;
  readOnly?: boolean;
  centreMap?: boolean;
  theme?: 'dark' | 'light';
  save?: SaveTarget | null;
  /** Shown as a footnote under the title. */
  notes?: string[];
}

declare global {
  interface Window {
    /** Read at load time by the automount below. */
    __KEPLER_MAP__?: MapSpec;
    /**
     * Declared optional, and it is: the property does not exist until the
     * bundle has finished evaluating. A page that loaded a stale or broken
     * bundle can therefore ask whether it is there, which is the check worth
     * making — assuming it is present turns a failed load into an obscure
     * property access further down.
     */
    KeplerViewer?: {mount: (spec: MapSpec) => void; version: string};
  }
}
