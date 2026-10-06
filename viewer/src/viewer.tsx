/**
 * The map page: a header, the map, and a Save button that means something.
 *
 * The header is not decoration. A kepler.gl map opened on its own gives no clue
 * whether it is connected to anything — whether editing it changes a record or
 * evaporates on reload. The header answers that in one line: what this map is,
 * where it came from, and whether the changes on screen have been written back.
 *
 * The save logic is the whole reason this bundle exists rather than an exported
 * static page. kepler keeps the edited configuration in its store, so "Save"
 * serializes that store and posts it; a static export would have the config
 * baked in and no way to read the edits out.
 */

// Named, not default. Every `@kepler.gl/*` package is CommonJS carrying
// `__esModule: true`, so esbuild's node-mode interop hands a default import the
// module namespace object rather than the export — React then fails with error
// #130, "expected a string ... or a class/function ... but got: object", which
// names the symptom but not the import.
import {KeplerGl} from '@kepler.gl/components';
import React, {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Provider} from 'react-redux';

import {configFingerprint, createKeplerStore, currentConfig, loadMap} from './store';
import type {MapSpec, SaveTarget} from './types';

/**
 * What the header says about saving.
 *
 * The time of the last successful save is deliberately *not* part of this. An
 * earlier version carried `at` on the `idle` variant only, so the transition
 * out of `dirty` had nothing to restore and the timestamp vanished the first
 * time the map was touched — the type was right and the intent was not. It
 * lives in its own piece of state, which does not change when the status does.
 */
type SaveState =
  | {kind: 'idle'}
  | {kind: 'dirty'}
  | {kind: 'saving'}
  | {kind: 'error'; message: string};

/**
 * How long the map must stop dispatching before the loaded config is taken as
 * the baseline. Long enough to cover the default layer being created and the
 * viewport being fitted to it, short enough that it is over before a user has
 * read the header.
 */
const SETTLE_MS = 400;

const STYLES = `
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; }
  body {
    font-family: ff-clan-web-pro, -apple-system, BlinkMacSystemFont, "Segoe UI",
      Helvetica, Arial, sans-serif;
    background: #12141a; color: #d5dae3;
  }
  #kepler-root { display: flex; flex-direction: column; height: 100%; }
  .kv-app { display: flex; flex-direction: column; height: 100%; min-height: 0; }
  .kv-header {
    display: flex; align-items: center; gap: 16px;
    padding: 10px 16px; background: #1b1f27;
    border-bottom: 1px solid #2c323d; flex: 0 0 auto;
  }
  .kv-heading { min-width: 0; flex: 1 1 auto; }
  .kv-title {
    font-size: 15px; font-weight: 600; color: #f0f3f7;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .kv-sub {
    font-size: 12px; color: #858e9d; margin-top: 2px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .kv-actions { display: flex; align-items: center; gap: 12px; flex: 0 0 auto; }
  .kv-status { font-size: 12px; color: #858e9d; }
  .kv-status[data-kind="error"] { color: #ff8a80; max-width: 320px; }
  .kv-status[data-kind="dirty"] { color: #ffcc66; }
  .kv-status[data-kind="idle"] { color: #7fd1a6; }
  .kv-save {
    font: inherit; font-size: 13px; font-weight: 600;
    padding: 7px 16px; border-radius: 6px; cursor: pointer;
    border: 1px solid #3a4250; background: #2b62d9; color: #fff;
    transition: background .15s ease, opacity .15s ease;
  }
  .kv-save:hover:not(:disabled) { background: #3a72ea; }
  .kv-save:disabled { opacity: .45; cursor: default; }
  .kv-map { flex: 1 1 auto; position: relative; min-height: 0; }
  .kv-error {
    margin: 24px auto; max-width: 640px; padding: 20px 24px;
    background: #2a1c1e; border: 1px solid #5c2b2f; border-radius: 8px;
    font-size: 13px; line-height: 1.6; color: #ffb4ab;
  }
  .kv-error code {
    display: block; margin-top: 10px; padding: 10px; overflow-x: auto;
    background: #16191f; border-radius: 4px; color: #d5dae3;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
`;

function formatTime(at: Date): string {
  return at.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
}

/**
 * Post the current config to the host.
 *
 * `keepalive` is set so a save survives the tab closing underneath it — the
 * classic way to lose an edit is to hit Save and immediately navigate away.
 */
async function postConfig(target: SaveTarget, body: unknown): Promise<void> {
  const response = await fetch(target.url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', ...(target.headers ?? {})},
    body: JSON.stringify(body),
    credentials: 'same-origin',
    keepalive: true
  });
  if (response.ok) return;
  // Read the body, because a host that refuses says why in it and the reason is
  // the only thing that makes the failure actionable.
  let detail = `${response.status} ${response.statusText}`;
  try {
    const text = (await response.text()).trim();
    if (text) detail = text.slice(0, 400);
  } catch {
    // Body already consumed or unreadable; the status line stands on its own.
  }
  throw new Error(detail);
}

/**
 * The map area's size in CSS pixels, tracked as the window changes.
 *
 * kepler.gl will not size itself to its parent. `<KeplerGl>` writes
 * `width: 800px; height: 800px` — its own defaults — into the style attribute of
 * the element it also hands to a ResizeObserver, so the observer can only ever
 * report the size kepler just set: the loop is closed and the map stays 800×800
 * inside whatever container it was given. The fix is to measure the container
 * ourselves and pass the numbers in as props, which kepler does honour, and to
 * keep passing them as the window resizes.
 *
 * Returns null until the first measurement, so the map is not mounted at the
 * wrong size for a frame and then resized.
 */
function useContainerSize(
  ref: React.RefObject<HTMLElement | null>
): {width: number; height: number} | null {
  const [size, setSize] = useState<{width: number; height: number} | null>(null);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const measure = (): void => {
      const rect = element.getBoundingClientRect();
      const next = {width: Math.round(rect.width), height: Math.round(rect.height)};
      if (next.width < 1 || next.height < 1) return;
      setSize(previous =>
        previous && previous.width === next.width && previous.height === next.height
          ? previous
          : next
      );
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref]);

  return size;
}

interface ViewerProps {
  spec: MapSpec;
  onError: (message: string) => void;
}

function Viewer({spec, onError}: ViewerProps): React.ReactElement {
  const store = useMemo(
    () => createKeplerStore(spec.mapId, Boolean(spec.readOnly)),
    [spec.mapId, spec.readOnly]
  );
  const [loaded, setLoaded] = useState(false);
  const [datasetCount, setDatasetCount] = useState(0);
  const [save, setSave] = useState<SaveState>({kind: 'idle'});
  const [lastSavedAt, setLastSavedAt] = useState<Date | null>(null);
  //: The config as last written. Compared against the live one to decide
  //: whether there is anything to save; held in a ref because it is read from
  //: the store subscription, which must not re-subscribe when it changes.
  const saved = useRef<string>('');
  const mapArea = useRef<HTMLDivElement | null>(null);
  const size = useContainerSize(mapArea);

  useEffect(() => {
    let cancelled = false;
    loadMap(store, spec)
      .then(count => {
        if (cancelled) return;
        setDatasetCount(count);
        setLoaded(true);
      })
      .catch((error: unknown) => {
        if (!cancelled) onError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
    };
  }, [store, spec, onError]);

  useEffect(() => {
    if (!loaded || !spec.save) return;

    // The baseline is taken when the map goes quiet, not when `loadMap`
    // resolves. Those are seconds apart in what they measure: resolving means
    // the action was dispatched, and kepler then creates the default layer in a
    // follow-up task and fits the viewport to it. Baselining at the earlier
    // point compares a config with no layers against one with a layer and
    // reports "Unsaved changes" on a map nobody has touched — which is how a
    // user learns to ignore the indicator.
    //
    // The window is restarted by each dispatch, so it closes only after kepler
    // has genuinely stopped, and it is one-shot: after it fires, edits are
    // tracked normally. A window that stayed open would swallow a real edit
    // made in a pause.
    let settleTimer: ReturnType<typeof setTimeout> | null = null;
    let baselining = true;
    const finishBaseline = (): void => {
      settleTimer = null;
      baselining = false;
      saved.current = configFingerprint(currentConfig(store, spec.mapId));
      setSave(previous => (previous.kind === 'dirty' ? {kind: 'idle'} : previous));
    };

    // kepler dispatches on every mouse move over the map, so this fires far
    // more often than the config actually changes. The fingerprint comparison
    // is what keeps it from being a re-render per frame: the state only moves
    // when the serialized config differs from the last one.
    const unsubscribe = store.subscribe(() => {
      if (baselining) {
        if (settleTimer) clearTimeout(settleTimer);
        settleTimer = setTimeout(finishBaseline, SETTLE_MS);
        return;
      }
      const fingerprint = configFingerprint(currentConfig(store, spec.mapId));
      setSave(previous => {
        const isDirty = fingerprint !== saved.current;
        if (previous.kind === 'dirty' && isDirty) return previous;
        // A save in flight owns the status. An error stays until the next
        // attempt, because it is still true until then, and clearing it on the
        // next keystroke would hide a failure the user has not yet seen.
        if (previous.kind === 'saving' || previous.kind === 'error') return previous;
        if (isDirty) return {kind: 'dirty'};
        if (previous.kind === 'dirty') return {kind: 'idle'};
        return previous;
      });
    });

    // Armed here as well as on dispatch, because a map that loads with a saved
    // config and no auto-created layer may not dispatch again at all.
    settleTimer = setTimeout(finishBaseline, SETTLE_MS);

    return () => {
      unsubscribe();
      if (settleTimer) clearTimeout(settleTimer);
    };
  }, [store, spec.mapId, spec.save, loaded]);

  const onSave = useCallback(async () => {
    if (!spec.save) return;
    const config = currentConfig(store, spec.mapId);
    if (!config) return;
    setSave({kind: 'saving'});
    try {
      await postConfig(spec.save, {mapId: spec.mapId, config});
      saved.current = configFingerprint(config);
      setLastSavedAt(new Date());
      setSave({kind: 'idle'});
    } catch (error: unknown) {
      setSave({
        kind: 'error',
        message: error instanceof Error ? error.message : String(error)
      });
    }
  }, [store, spec.mapId, spec.save]);

  const status = useMemo(() => {
    if (!spec.save) return {text: 'Local map — not connected to a server', kind: 'plain'};
    switch (save.kind) {
      case 'saving':
        return {text: 'Saving…', kind: 'plain'};
      case 'dirty':
        return {text: 'Unsaved changes', kind: 'dirty'};
      case 'error':
        return {text: `Save failed — ${save.message}`, kind: 'error'};
      default:
        return {
          text: lastSavedAt ? `Saved at ${formatTime(lastSavedAt)}` : 'Saved',
          kind: 'idle'
        };
    }
  }, [save, spec.save, lastSavedAt]);

  const subtitle = useMemo(() => {
    const parts = [
      `${datasetCount} dataset${datasetCount === 1 ? '' : 's'}`,
      ...(spec.notes ?? [])
    ];
    return parts.join(' · ');
  }, [datasetCount, spec.notes]);

  return (
    <div className="kv-app">
      <header className="kv-header">
        <div className="kv-heading">
          <div className="kv-title">{spec.title || 'Kepler.gl map'}</div>
          {subtitle ? <div className="kv-sub">{subtitle}</div> : null}
        </div>
        <div className="kv-actions">
          <span className="kv-status" data-kind={status.kind}>
            {status.text}
          </span>
          {spec.save ? (
            <button
              type="button"
              className="kv-save"
              onClick={onSave}
              disabled={save.kind === 'saving' || save.kind === 'idle'}
            >
              {spec.save.label || 'Save map'}
            </button>
          ) : null}
        </div>
      </header>
      <div className="kv-map" ref={mapArea}>
        {size ? (
          <Provider store={store}>
            {/* `mint={false}` because `createKeplerStore` has already registered
                this instance and loaded the data into it. The default, `true`,
                mints a fresh empty instance on mount — which would discard the
                datasets and the saved config on the way in. */}
            <KeplerGl
              id={spec.mapId}
              mint={false}
              width={size.width}
              height={size.height}
            />
          </Provider>
        ) : null}
      </div>
    </div>
  );
}

/** Render the map, or the reason it could not be rendered. */
export function mount(spec: MapSpec): void {
  const root = document.getElementById('kepler-root') ?? document.body;
  const react = createRoot(root);

  const fail = (message: string): void => {
    console.error('[kepler-viewer]', message);
    react.render(
      <div className="kv-error">
        <strong>The map could not be rendered.</strong>
        <code>{message}</code>
      </div>
    );
  };

  // The style tag is added before the first render rather than by an effect,
  // so the page does not paint an unstyled map for a frame and then reflow.
  if (!document.getElementById('kepler-viewer-styles')) {
    const tag = document.createElement('style');
    tag.id = 'kepler-viewer-styles';
    tag.textContent = STYLES;
    document.head.appendChild(tag);
  }

  try {
    react.render(<Viewer spec={spec} onError={fail} />);
  } catch (error: unknown) {
    fail(error instanceof Error ? error.message : String(error));
  }
}

export {STYLES as viewerStyles};
