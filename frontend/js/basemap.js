// Basemaps (ADR 0015): what a map draws under its markers, chosen per browser from those the
// instance has. The `settings` event's `map` field (the backend's camera_viewer.maps) says which
// those are, `{ extract, tiles, imagery }`, and BASEMAPS below describes each one as plain data: the `id`
// view state saves, the `name` the switcher shows, whether `settings.map` offers it, and how to
// build it as a map.js basemap object (null meaning map.js's own BLANK). Adding a Basemap is one
// more entry there. Blank is always offered, so there is always something to draw.
//
// The choice is the view-state key `basemap` (view-state.js), shared by every map in the page
// and kept per browser. With no saved choice, or a saved one this instance doesn't offer, a
// browser gets the first of DEFAULT_ORDER the instance has, which was the only order before
// there was a choice. Falling back leaves the saved choice alone, so it comes back if the
// instance offers it again; only the viewer choosing replaces it. A Basemap that fails to build
// draws blank, because the map is never broken, only plainer.
//
// `chosenBasemap(store)` builds the Basemap to create a map with, and
// `mountBasemapSwitcher(store, mapHandle, id)` then keeps that map (a map.js handle) on the
// choice and gives it the switcher, a <select> named "Basemap" in its top-left corner, when the
// instance has more than one Basemap.
//
// An online tile server (CV_MAP_TILES) is `map.tiles`, `{ url, attribution }`: a raster tile URL
// template MapLibre fills with {z}/{x}/{y}, checked by the backend at startup, and the plain-text
// attribution the operator gave for it (OpenStreetMap's by default), escaped here so it shows as
// written. Its tiles come from that server, not the instance, so they work only online.
//
// A Map Extract is one .pmtiles file the instance serves at `map.extract`, read by pmtiles.js
// (vendored, ADR 0011) through Range requests, drawn with the Protomaps light style and its
// glyphs and sprites, all vendored too, so it renders with no network at all. pmtiles.js is a
// classic script that sets a global `pmtiles`, so it is loaded with a <script> element, once,
// and only when there is an extract to read.
//
// 3D buildings (#84) are the extract's alone, since only it has building heights: its `buildings`
// layer carries `height` from zoom 11 to 15 and building parts' `min_height` from 14. The toggle,
// the view-state key `buildings3d`, sits beside the switcher and shows only while the chosen
// Basemap has `buildings`; it stays saved while another Basemap is drawn, so 3D comes back with
// Streets. The 3D variant of a Basemap is another build: a `fill-extrusion` layer over the
// vendored style's flat buildings, in their colour, and a `pitch` map.js tilts the map to. The
// style file itself stays as published. A building without a height stays flat, rather than
// standing at a guessed one.

const VENDOR = '/vendor';
const STYLE = `${VENDOR}/protomaps-basemaps/style-light.json`;
const ASSETS = `${VENDOR}/protomaps-basemaps-assets`;
const PMTILES = `${VENDOR}/pmtiles/pmtiles.js`;
// OpenStreetMap data is ODbL, which requires this attribution wherever the map shows its tiles.
const EXTRACT_ATTRIBUTION = [
  '<a href="https://github.com/protomaps/basemaps" target="_blank" rel="noopener">Protomaps</a>',
  '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>',
  'contributors,',
  '<a href="https://opendatacommons.org/licenses/odbl/" target="_blank" rel="noopener">ODbL</a>',
].join(' ');

// The tilt of a map showing 3D buildings, in degrees from straight down (map.js's `pitch`).
const BUILDINGS_PITCH = 50;

let pmtilesLoading = null;
const loadPmtiles = () =>
  (pmtilesLoading ??= new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = PMTILES;
    script.addEventListener('load', () => resolve(window.pmtiles));
    script.addEventListener('error', () => {
      pmtilesLoading = null;
      script.remove();
      reject(new Error(`could not load ${PMTILES}`));
    });
    document.head.append(script);
  }));

// One Protocol for the page, so every build of the extract shares its cache of the file's header
// and directories.
let protocol = null;

// The style's layers with an extruded copy of its flat `buildings` layer, in that layer's colour,
// added under the first label so names stay readable above the roofs.
function withBuildings(layers) {
  const flat = layers.find((layer) => layer.id === 'buildings');
  const extruded = {
    id: 'buildings-3d',
    type: 'fill-extrusion',
    source: flat.source,
    'source-layer': flat['source-layer'],
    filter: flat.filter,
    paint: {
      'fill-extrusion-color': flat.paint['fill-color'],
      'fill-extrusion-height': ['coalesce', ['get', 'height'], 0],
      'fill-extrusion-base': ['coalesce', ['get', 'min_height'], 0],
      'fill-extrusion-opacity': 0.9,
    },
  };
  const firstLabel = layers.findIndex((layer) => layer.type === 'symbol');
  const at = firstLabel === -1 ? layers.length : firstLabel;
  return [...layers.slice(0, at), extruded, ...layers.slice(at)];
}

async function extractBasemap(url, buildings3d) {
  const [pmtiles, style] = await Promise.all([
    loadPmtiles(),
    fetch(STYLE).then((response) => {
      if (!response.ok) throw new Error(`${STYLE} answered ${response.status}`);
      return response.json();
    }),
  ]);
  protocol ??= new pmtiles.Protocol();
  // map.js draws the background itself, from the design tokens.
  const layers = style.layers.filter((layer) => layer.type !== 'background');
  // Full URLs, built by hand: URL() would escape the {fontstack} and {range} tokens.
  const origin = window.location.origin;
  return {
    sources: {
      ...style.sources,
      protomaps: { ...style.sources.protomaps, url: `pmtiles://${origin}${url}`, attribution: EXTRACT_ATTRIBUTION },
    },
    layers: buildings3d ? withBuildings(layers) : layers,
    glyphs: `${origin}${ASSETS}/fonts/{fontstack}/{range}.pbf`,
    sprite: `${origin}${ASSETS}/sprites/v4/light`,
    protocols: { pmtiles: protocol.tile },
    grid: false,
    ...(buildings3d ? { pitch: BUILDINGS_PITCH } : {}),
  };
}

const escapeHtml = (text) =>
  text.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

// A raster server's tiles to level 19, as deep as the common ones go. One that stops sooner,
// such as NAIP at 16, answers 404 below its last level, and MapLibre then draws the deepest tile
// it got enlarged, so the closest zooms turn blurry rather than blank.
function tilesBasemap({ url, attribution }) {
  return {
    sources: {
      online: { type: 'raster', tiles: [url], tileSize: 256, maxzoom: 19, attribution: escapeHtml(attribution) },
    },
    layers: [{ id: 'online', type: 'raster', source: 'online' }],
    grid: false,
  };
}

// Every Basemap, in the order the switcher lists them. `offered(settings.map)` says whether the
// instance has it; `build(settings.map, buildings3d)` returns (or resolves to) a map.js basemap,
// or null for BLANK. `buildings` marks the one that can draw 3D buildings, which `build` then
// draws when `buildings3d` is true.
const BASEMAPS = [
  {
    id: 'extract',
    name: 'Streets',
    buildings: true,
    offered: (map) => Boolean(map?.extract),
    build: (map, buildings3d) => extractBasemap(map.extract, buildings3d),
  },
  { id: 'tiles', name: 'Streets online', offered: (map) => Boolean(map?.tiles), build: (map) => tilesBasemap(map.tiles) },
  // CV_MAP_IMAGERY (#83): `map.imagery` is a raster server shaped like `map.tiles`, never a default.
  { id: 'imagery', name: 'Satellite', offered: (map) => Boolean(map?.imagery), build: (map) => tilesBasemap(map.imagery) },
  { id: 'blank', name: 'Blank', offered: () => true, build: () => null },
];
// What a browser draws until it chooses: the first of these the instance has.
const DEFAULT_ORDER = ['extract', 'tiles', 'blank'];

// The Basemaps `settings.map` offers, in switcher order.
const offeredBy = (map) => BASEMAPS.filter((basemap) => basemap.offered(map));

// The saved choice if it is offered, else the default.
const chosenOf = (offered, saved) =>
  offered.find((basemap) => basemap.id === saved)
  ?? DEFAULT_ORDER.map((id) => offered.find((basemap) => basemap.id === id)).find(Boolean);

// Whether `basemap` draws 3D buildings for the saved toggle: only one that has them.
const in3d = (basemap, buildings3d) => Boolean(basemap.buildings && buildings3d);

// What a map is drawing: a Basemap's id, with ':3d' when it draws 3D buildings.
const drawingOf = (basemap, buildings3d) => (in3d(basemap, buildings3d) ? `${basemap.id}:3d` : basemap.id);

// Each Basemap, flat and in 3D, is built once per page, since `settings.map` is fixed for the
// instance's lifetime; a failed build is forgotten so a later choice tries again.
const built = new Map(); // drawingOf -> Promise of a map.js basemap or null
function build(basemap, map, buildings3d) {
  const key = drawingOf(basemap, buildings3d);
  if (!built.has(key)) {
    built.set(key, Promise.resolve().then(() => basemap.build(map, in3d(basemap, buildings3d))).catch((error) => {
      built.delete(key);
      console.warn(`the ${basemap.name} Basemap is unavailable, drawing the blank map instead:`, error);
      return null;
    }));
  }
  return built.get(key);
}

// The Basemap this browser draws now, for createMap: `{ id, basemap }`, where `basemap` is a
// map.js basemap or null for BLANK, and `id` says which, 3D or not, for mountBasemapSwitcher.
// Needs `settings` in the store.
export async function chosenBasemap(store) {
  const map = store.get('settings').map;
  const basemap = chosenOf(offeredBy(map), store.get('basemap'));
  const buildings3d = store.get('buildings3d');
  return { id: drawingOf(basemap, buildings3d), basemap: await build(basemap, map, buildings3d) };
}

// Keep `mapHandle`, a map.js handle created with the Basemap `shown` (an id from chosenBasemap),
// on the browser's choice, and put the switcher on it when there is more than one Basemap to choose,
// with the 3D toggle beside it while the chosen one has 3D buildings.
export function mountBasemapSwitcher(store, mapHandle, shown) {
  const map = store.get('settings').map;
  const offered = offeredBy(map);
  const select = document.createElement('select');
  select.className = 'map-basemap';
  select.setAttribute('aria-label', 'Basemap');
  select.title = 'Basemap';
  select.append(...offered.map((basemap) => new Option(basemap.name, basemap.id)));
  select.addEventListener('change', () => store.set('basemap', select.value));
  const buildings = document.createElement('button');
  buildings.type = 'button';
  buildings.className = 'map-buildings';
  buildings.textContent = '3D';
  buildings.setAttribute('aria-label', '3D buildings');
  buildings.title = '3D buildings';
  buildings.addEventListener('click', () => store.set('buildings3d', !store.get('buildings3d')));
  if (offered.length > 1) {
    const group = document.createElement('div');
    group.className = 'maplibregl-ctrl maplibregl-ctrl-group';
    group.append(select, buildings);
    mapHandle.addControl(group, 'top-left');
  }

  let wanted = shown;
  const drawChoice = () => {
    const basemap = chosenOf(offered, store.get('basemap'));
    const buildings3d = store.get('buildings3d');
    select.value = basemap.id;
    buildings.hidden = !basemap.buildings;
    buildings.setAttribute('aria-pressed', String(Boolean(buildings3d)));
    const drawing = drawingOf(basemap, buildings3d);
    if (drawing === wanted) return;
    wanted = drawing;
    build(basemap, map, buildings3d).then((drawn) => {
      if (wanted === drawing) mapHandle.setBasemap(drawn); // unless chosen away while it built
    });
  };
  store.on('basemap', drawChoice);
  store.on('buildings3d', drawChoice);
}
