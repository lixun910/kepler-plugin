/**
 * Turning a dataset spec into something kepler will accept.
 *
 * Two jobs, and the second is the one that bites.
 *
 * **Inline rows pass straight through.** They arrive as JSON from the Python
 * side, which has already normalised them.
 *
 * **Parquet has to be decoded, and the decoder returns BigInt.** hyparquet
 * maps the 64-bit physical types — `INT64`, `TIMESTAMP`, `UINT64` — onto
 * JavaScript `BigInt`, because that is the only type that holds the full range
 * without silently losing precision. kepler and `JSON.stringify` both refuse a
 * BigInt, so `normalise` converts them: to a number when it round-trips, and to
 * a string when it does not. A string is the honest answer for an id too large
 * to survive as a double, and it costs nothing that was not already lost.
 *
 * A `_geojson` column is the plugin's own convention for geometry: one GeoJSON
 * geometry per row, as a JSON string. It is what makes a `geojson` dataset
 * renderable by kepler, whose geojson layers read a FeatureCollection rather
 * than a column of WKB.
 */

import {processGeojson, processRowObject} from '@kepler.gl/processors';
import {parquetReadObjects} from 'hyparquet';
import {asyncBufferFromUrl, byteLengthFromUrl} from 'hyparquet/src/utils.js';

import type {DatasetSpec} from './types';

/** The column the plugin writes GeoJSON geometry into. */
export const GEOJSON_COLUMN = '_geojson';

/**
 * kepler's own dataset shape, as `addDataToMap` wants it.
 *
 * `data` is `{fields, rows}` — a column list and a row-major array of arrays —
 * and not the array of plain objects it looks like it should be. That is worth
 * stating because getting it wrong fails in a way that names nothing useful:
 * `createNewDataEntry` runs `validateInputData`, which requires `fields` to be
 * an array, and rejects the payload with the toast "Failed to create a new
 * dataset due to data verification errors". Nothing says which check failed or
 * that the caller was supposed to have run a processor first.
 */
export interface KeplerDataset {
  info: {id: string; label: string; format: 'row' | 'geojson'};
  data: {fields: unknown[]; rows: unknown[][]};
}

/**
 * Replace the values kepler and JSON cannot carry.
 *
 * Applied to a whole row at a time rather than to a column, because a parquet
 * file's types are per column and the row is what gets walked once.
 */
function normaliseRow(row: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(row)) {
    out[key] = normaliseValue(value);
  }
  return out;
}

function normaliseValue(value: unknown): unknown {
  if (typeof value === 'bigint') {
    // Number.MAX_SAFE_INTEGER, inclusive. Beyond it the conversion is lossy,
    // and a lossy id renders as a different id — a string does not.
    if (value >= -9007199254740991n && value <= 9007199254740991n) return Number(value);
    return value.toString();
  }
  if (value === null || value === undefined) return value;
  // Parquet LIST and STRUCT columns arrive as arrays and plain objects.
  if (Array.isArray(value)) return value.map(normaliseValue);
  if (value instanceof Date) return value.toISOString();
  if (typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      out[k] = normaliseValue(v);
    }
    return out;
  }
  return value;
}

/** Fetch and decode a Parquet object into plain rows. */
async function readParquet(url: string): Promise<Array<Record<string, unknown>>> {
  // The byte length is asked for separately so hyparquet can issue ranged reads
  // against the footer instead of pulling the whole object to find the schema.
  // For a hosted dataset that is the difference between one small request and
  // a full download, which is also what the user is billed for.
  const byteLength = await byteLengthFromUrl(url);
  const file = await asyncBufferFromUrl({url, byteLength});
  const rows = await parquetReadObjects({file});
  return (rows as Array<Record<string, unknown>>).map(normaliseRow);
}

/** Build the FeatureCollection a kepler geojson layer needs, from `_geojson`. */
function toFeatureCollection(
  rows: Array<Record<string, unknown>>
): GeoJSON.FeatureCollection {
  const features: GeoJSON.Feature[] = [];
  for (const row of rows) {
    const raw = row[GEOJSON_COLUMN];
    if (typeof raw !== 'string' || !raw) continue;
    let geometry: unknown;
    try {
      geometry = JSON.parse(raw);
    } catch {
      // One malformed geometry must not cost the whole dataset. Skipping the
      // row keeps the rest of the map, and the count is reported by the caller.
      continue;
    }
    if (!geometry) continue;
    const properties: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(row)) {
      if (key !== GEOJSON_COLUMN) properties[key] = value;
    }
    features.push({
      type: 'Feature',
      geometry: geometry as GeoJSON.Geometry,
      properties
    });
  }
  return {type: 'FeatureCollection', features} as GeoJSON.FeatureCollection & {
    name?: string;
  };
}

/** Resolve one dataset spec to rows, from wherever the spec says they are. */
async function rowsFor(spec: DatasetSpec): Promise<Array<Record<string, unknown>>> {
  if ('rows' in spec && Array.isArray(spec.rows)) return spec.rows.map(normaliseRow);
  if ('parquetUrl' in spec && spec.parquetUrl) return readParquet(spec.parquetUrl);
  return [];
}

/**
 * Load every dataset in the spec.
 *
 * Datasets are fetched together rather than in sequence: a map with three
 * parquet datasets would otherwise wait three round trips end to end, and
 * nothing about the decoding depends on the order.
 *
 * The `@kepler.gl/processors` call is the caller's job, not kepler's.
 * `addDataToMap` advertises a `format` per dataset and a `DATASET_HANDLERS`
 * table keyed by it, which reads as though a `'row'` dataset is processed on
 * the way in. It is not: `createNewDataEntry` validates the payload as it
 * arrives, so an unprocessed dataset fails verification. Running the processor
 * here also means the field types are inferred once, by kepler's own analyzer,
 * and the result is what a saved config refers to by name.
 */
export async function loadDatasets(specs: DatasetSpec[]): Promise<KeplerDataset[]> {
  const loaded = await Promise.all(
    specs.map(async spec => {
      const rows = await rowsFor(spec);
      if (spec.kind === 'geojson') {
        return {
          info: {id: spec.id, label: spec.label, format: 'geojson' as const},
          data: processGeojson(toFeatureCollection(rows)) as KeplerDataset['data']
        };
      }
      return {
        info: {id: spec.id, label: spec.label, format: 'row' as const},
        data: processRowObject(rows) as KeplerDataset['data']
      };
    })
  );
  return loaded.filter(dataset => {
    // An empty dataset is not worth handing to kepler: it becomes a layer that
    // renders nothing and a legend with no entries, which reads as a bug.
    return (dataset.data?.rows?.length ?? 0) > 0;
  });
}
