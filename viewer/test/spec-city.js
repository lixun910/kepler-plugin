// A second fixture, and the one that answers two questions the smoke test
// cannot.
//
// The first is whether the basemap draws at all. The smoke test fits the view
// to six cities spread across the United States — around zoom 3.5, where
// CARTO's dark-matter style is close to uniformly black, so a working basemap
// and a broken one look the same. This one is pinned to San Francisco at zoom
// 11, where streets are unmistakable if they are being fetched.
//
// The second, and the reason this is a fixture rather than a throwaway: it
// loads a *saved config* rather than letting kepler guess. `config` is exactly
// the shape the Save button produces and the server stores, so this is the
// round trip in reverse — the half that decides whether "change the map, save
// it, open it again" actually holds.
window.__KEPLER_MAP__ = {
  mapId: 'city',
  title: 'Saved-config fixture — San Francisco',
  centreMap: false,
  datasets: [
    {
      id: 'sf',
      label: 'SF points',
      kind: 'point',
      rows: [
        {lat: 37.7749, lng: -122.4194, name: 'Civic Center', value: 5},
        {lat: 37.8083, lng: -122.4098, name: 'Pier 39', value: 9},
        {lat: 37.7694, lng: -122.4862, name: 'Golden Gate Park', value: 3}
      ]
    }
  ],
  config: {
    version: 'v1',
    config: {
      visState: {
        filters: [],
        layers: [
          {
            id: 'sf-saved-layer',
            type: 'point',
            config: {
              dataId: 'sf',
              label: 'Saved layer',
              color: [255, 153, 31],
              columns: {lat: 'lat', lng: 'lng', altitude: null},
              isVisible: true,
              visConfig: {radius: 30, opacity: 0.9, outline: true}
            },
            visualChannels: {
              colorField: null,
              colorScale: 'quantile',
              sizeField: 'value',
              sizeScale: 'linear'
            }
          }
        ],
        effects: [],
        interactionConfig: {},
        layerBlending: 'normal',
        overlayBlending: 'normal',
        splitMaps: [],
        animationConfig: {currentTime: null, speed: 1},
        editor: {mode: 'RECEIVE', features: [], visible: true}
      },
      mapState: {
        bearing: 0,
        dragRotate: false,
        // The pin. `centreMap: false` above stops kepler refitting the view to
        // the data on load, so if the map ends up here, the config was applied.
        latitude: 37.7749,
        longitude: -122.4194,
        pitch: 0,
        zoom: 11,
        isSplit: false
      },
      mapStyle: {
        styleType: 'dark-matter',
        topLayerGroups: {},
        visibleLayerGroups: {label: true, road: true, border: true, building: true, water: true, land: true},
        threeDBuildingColor: [15, 15, 15],
        backgroundColor: [0, 0, 0],
        mapStyles: {}
      }
    }
  },
  save: {url: '/__save', label: 'Save map'}
};
