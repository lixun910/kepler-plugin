// The spec the smoke-test page mounts. Shared by index.html and debug.html so
// the two differ only in which build of the bundle they load.
window.__KEPLER_MAP__ = {
  mapId: 'smoke',
  title: 'kepler-viewer smoke test',
  centreMap: true,
  datasets: [
    {
      id: 'cities',
      label: 'Cities',
      kind: 'point',
      rows: [
        {lat: 37.7749, lng: -122.4194, name: 'San Francisco', value: 15},
        {lat: 34.0522, lng: -118.2437, name: 'Los Angeles', value: 42},
        {lat: 40.7128, lng: -74.006, name: 'New York', value: 27},
        {lat: 41.8781, lng: -87.6298, name: 'Chicago', value: 31},
        {lat: 29.7604, lng: -95.3698, name: 'Houston', value: 19},
        {lat: 39.9526, lng: -75.1652, name: 'Philadelphia', value: 23}
      ]
    }
  ],
  config: null,
  save: {url: '/__save', label: 'Save map'}
};
