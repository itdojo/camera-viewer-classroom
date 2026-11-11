// The map: MapLibre GL JS, vendored under ADR 0011, wrapped so the rest of the UI never touches
// MapLibre itself. It draws any number of markers, each pointing along its heading, over a
// basemap, and follows them until the viewer drags the map; `recentre()` turns follow back on.
// A lone marker is followed by centring it at whatever zoom the viewer chose. Several are
// followed by fitting them all in view, no closer than the starting zoom, but only when the map
// opens, when a marker joins, or when one is out of view; otherwise the viewer's zoom and centre
// stay, so zooming in on a group sticks until a marker leaves it. Recentre always fits them all.
// Single View uses one marker (single-view-map.js), Map View one per Device (map-view.js).
//
// A basemap is `{ sources, layers, glyphs?, sprite?, protocols?, pitch?, grid }`: MapLibre style
// sources and layers, drawn above the background and below everything this module adds. BLANK has none
// and asks for the coordinate grid, so motion and distance still read with no tiles at all. A Map
// Extract or an online tile server is another basemap object (basemap.js); a source's own
// `attribution` joins the attribution control, which is never collapsed, so it shows in every
// basemap mode. `protocols` maps a URL scheme its sources use to the loader MapLibre calls for it
// (`pmtiles` for a Map Extract), so no other module touches MapLibre to register one.
// `pitch` tilts the map that many degrees from straight down while the basemap shows, for one
// with 3D buildings; without it the map is flat and north up.
// `setBasemap(basemap)` swaps the basemap under a live map: the markers, their Trails, the view
// and follow all stay as they were, and the attribution follows the new basemap's sources. The
// tilt eases to the new basemap's pitch, or back to flat and north up.
// Which basemap to draw is basemap.js's business, which puts its switcher on the map with
// `addControl(element, corner)`.
//
// A marker is `{ id, label, latitude, longitude, heading, stale, source, since?, feedStatus?,
// trail? }` (a Position plus id and label), heading in degrees true or null. Its element is a
// role=img named "<label>: <lat>, <lon>, heading <n>°". A stale one is greyed and its name adds
// "last known", with `since` (its age, "12 s") after it when given; a Fixed Position
// (`source: "fixed"`) is drawn square and named so. `feedStatus` ('live', 'down'…) colours the
// marker, gives every status but Live a glyph, and ends the name ("Feed Status Down"). `trail`
// is the marker's Trail, `{ latitude, longitude }` points oldest first, drawn as one line layer
// above the basemap and grid and below the markers. The line is on the canvas, out of the DOM's
// reach, so the marker element's `data-trail` says how many points it holds once drawn (0 when
// it has fewer than two), which is what tests read.
//
// A marker with a `choose` function can be chosen: it is a focusable role=button, with the same
// name, and clicking it or pressing Enter or Space calls `choose(element)` with the marker's
// element, which stays the same node for as long as the marker is on the map (Map View opens
// its Map Preview, map-preview.js). A `description` ("Opens the Map Preview") is read after the
// name through `aria-describedby`, so the name stays the Position that tests and people read.
// An `address` joins the name after the label ("cam-2, 192.0.2.40: <lat>, <lon>, …").
//
// A marker with `detail: true` has a Marker Detail (Map View's): a card beside it with the
// label, the address, the coordinates, and "last known 12 s ago" or "Fixed Position" when either
// applies. It shows while a mouse or pen is over the marker or keyboard focus is on it, and on
// every such marker while `showDetails(true)`. It sits right of the marker, or left where the
// right would run past the map or under the padding, stays upright as the marker turns, and
// takes no pointer events, so a click on it reaches the map or the marker beneath. The one
// hovered or focused comes to the front. It is aria-hidden, since the name already says it all.
//
// `setPadding({ right, bottom })` keeps that many pixels along the right and bottom edges clear,
// for a panel laid over the map: follow and Recentre fit the markers into the rest, a marker
// under the panel counts as out of view, and MapLibre's controls in those corners move clear of
// it. What is on screen stays where it is when the padding changes, unless follow then refits.
//
// MapLibre's stylesheet belongs to it too, so this module adds it to the page when it first loads
// and finishes loading only once the stylesheet has. map-loader.js imports this module lazily,
// so a page that never shows a map fetches neither.

import * as maplibregl from '../vendor/maplibre-gl/maplibre-gl.mjs';
import { feedStatusLabel } from './feed-status.js';
import { coordinates, heading as headingText } from './position.js';

await new Promise((resolve) => {
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = new URL('../vendor/maplibre-gl/maplibre-gl.css', import.meta.url).href;
  link.addEventListener('load', resolve);
  link.addEventListener('error', resolve); // unstyled controls beat no map at all
  // Before the app's own stylesheet, where index.html used to link it, so the app's rules still win.
  const app = document.querySelector('link[rel="stylesheet"][href$="css/app.css"]');
  document.head.insertBefore(link, app);
});

export const BLANK = Object.freeze({ sources: {}, layers: [], grid: true });

const ATTRIBUTION = '<a href="https://maplibre.org/" target="_blank" rel="noopener">MapLibre</a>';
const START_ZOOM = 16;
const MAX_ZOOM = 19;
const FIT_PADDING = 64; // pixels kept clear around the markers when fitting them, at most
const DETAIL_OFFSET = 22; // pixels from a marker's middle to its Marker Detail: half a marker and 8
// Grid spacings in degrees, coarse to fine; the grid takes the coarsest giving 3 lines or more.
// Grid labels keep clear of each other's edge and of the controls in the corners (pixels).
const LABEL_CLEARANCE = { top: 24, bottom: 36, left: 96, right: 64 };
const GRID_STEPS = [10, 5, 2, 1, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002, 0.001, 0.0005, 0.0002, 0.0001, 0.00005];

function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// The paint colours this module draws from design tokens, re-read when the theme switches.
const THEMED = { background: '--surface-card', grid: '--border-strong', trail: '--jade' };

function gridStep(span) {
  return GRID_STEPS.find((step) => span / step >= 3) ?? GRID_STEPS.at(-1);
}

// 51.5005 -> "51.5005°N", -0.1235 -> "0.1235°W", 0 -> "0°", with as many decimals as the step.
function gridLabel(value, step, positive, negative) {
  const decimals = Math.max(0, Math.ceil(-Math.log10(step) - 1e-9));
  const text = Math.abs(value).toFixed(decimals);
  if (Number(text) === 0) return `${text}°`;
  return `${text}°${value > 0 ? positive : negative}`;
}

function gridValues(min, max, step) {
  const values = [];
  for (let k = Math.ceil(min / step); k * step <= max; k += 1) values.push(k * step);
  return values;
}

function style(basemap) {
  return {
    version: 8,
    ...(basemap.glyphs ? { glyphs: basemap.glyphs } : {}),
    ...(basemap.sprite ? { sprite: basemap.sprite } : {}),
    sources: {
      ...basemap.sources,
      grid: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
      trails: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
    },
    layers: [
      { id: 'background', type: 'background', paint: { 'background-color': token(THEMED.background) } },
      ...basemap.layers,
      ...(basemap.grid
        ? [{ id: 'grid', type: 'line', source: 'grid', paint: { 'line-color': token(THEMED.grid), 'line-width': 1 } }]
        : []),
      {
        id: 'trail',
        type: 'line',
        source: 'trails',
        layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint: { 'line-color': token(THEMED.trail), 'line-width': 3, 'line-opacity': 0.7 },
      },
    ],
  };
}

function markerElement(selectable) {
  const element = document.createElement('div');
  element.className = 'map-marker';
  element.setAttribute('role', selectable ? 'button' : 'img');
  if (selectable) element.tabIndex = 0;
  const arrow = document.createElement('span');
  arrow.className = 'map-marker-arrow';
  const status = document.createElement('span'); // the Feed Status glyph, drawn by app.css
  status.className = 'map-marker-status';
  status.setAttribute('aria-hidden', 'true');
  element.append(arrow, status);
  return element;
}

// A new MapLibre marker for `spec` on `map`. A selectable one calls `choose()` on a click, Enter
// or Space; listeners on the element itself survive `addTo`, which removes the marker first.
function addMarker(map, spec, choose) {
  const selectable = Boolean(spec.choose);
  const element = markerElement(selectable);
  if (selectable) {
    element.addEventListener('click', choose);
    element.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault(); // Space would scroll the page
      choose();
    });
  }
  return new maplibregl.Marker({ element, rotationAlignment: 'map' })
    .setLngLat([spec.longitude, spec.latitude])
    .addTo(map);
}

// A Marker Detail on `map` at `spec`'s Position: a point-sized MapLibre marker of its own, so it
// moves with the marker without turning with it, holding the card. `update(spec)` rewrites its
// lines, `show(shown, front)` shows or hides it and brings it to the front, and `place(limit)`
// puts the card on the left when on the right it would pass `limit`, an x in map pixels.
function addDetail(map, spec) {
  const element = document.createElement('div');
  element.className = 'map-detail';
  element.setAttribute('aria-hidden', 'true');
  element.hidden = true;
  const card = document.createElement('div');
  card.className = 'map-detail-card';
  const [label, address, place, note] = ['label', 'address', 'coordinates', 'note'].map((part) => {
    const line = document.createElement('span');
    line.className = `map-detail-${part}`;
    return line;
  });
  card.append(label, address, place, note);
  element.append(card);
  const marker = new maplibregl.Marker({ element }).setLngLat([spec.longitude, spec.latitude]).addTo(map);
  const write = (line, text) => {
    if (line.textContent !== text) line.textContent = text;
    line.hidden = !text;
  };
  return {
    marker,
    update(next) {
      marker.setLngLat([next.longitude, next.latitude]);
      write(label, next.label);
      write(address, next.address ?? '');
      write(place, coordinates(next));
      if (next.stale) write(note, next.since ? `last known ${next.since} ago` : 'last known');
      else write(note, next.source === 'fixed' ? 'Fixed Position' : '');
    },
    show(shown, front) {
      element.hidden = !shown;
      element.toggleAttribute('data-front', front);
    },
    place(limit) {
      if (element.hidden) return;
      const { x } = map.project(marker.getLngLat());
      const width = card.offsetWidth;
      const left = x + DETAIL_OFFSET + width > limit && x - DETAIL_OFFSET - width >= 0;
      card.dataset.side = left ? 'left' : 'right';
    },
  };
}

function addProtocols(basemap) {
  for (const [scheme, load] of Object.entries(basemap.protocols ?? {})) maplibregl.addProtocol(scheme, load);
}

let descriptionCount = 0; // marker description ids stay unique with two maps on the page

// Draw a map into `container`. Returns its handle: `setMarkers(markers)`, `recentre()`,
// `resize()`, `showDetails(shown)`, `setPadding(insets)`, `setBasemap(basemap)` and
// `addControl(element, corner)`.
export function createMap(container, { basemap: initial, title = 'Map' } = {}) {
  let basemap = initial ?? BLANK;
  addProtocols(basemap);
  const map = new maplibregl.Map({
    container,
    style: style(basemap),
    center: [0, 0],
    zoom: START_ZOOM,
    pitch: basemap.pitch ?? 0,
    maxZoom: MAX_ZOOM,
    attributionControl: false,
    dragRotate: false,
    pitchWithRotate: false,
    touchPitch: false,
    locale: { 'Map.Title': title },
  });
  map.touchZoomRotate.disableRotation();
  map.keyboard.disableRotation();
  map.addControl(new maplibregl.AttributionControl({ compact: false, customAttribution: ATTRIBUTION }), 'bottom-right');
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');

  const labels = document.createElement('div');
  labels.className = 'map-grid-labels';
  labels.setAttribute('aria-hidden', 'true');
  labels.hidden = !basemap.grid;
  container.append(labels);

  // One hidden element per marker description, which markers point at with aria-describedby.
  const descriptions = new Map();
  const describedBy = (text) => {
    if (!descriptions.has(text)) {
      const node = document.createElement('span');
      node.id = `map-description-${(descriptionCount += 1)}`;
      node.hidden = true;
      node.textContent = text;
      container.append(node);
      descriptions.set(text, node.id);
    }
    return descriptions.get(text);
  };

  // Whether the style has loaded, so its grid and trails sources exist. Not map.isStyleLoaded():
  // that is false while any source reloads, which the grid's own setData makes it on every move,
  // so the labels would lag a drag. False again while setBasemap loads the next style.
  let ready = false;
  const drawGrid = () => {
    if (!basemap.grid || !ready) return;
    const bounds = map.getBounds();
    const [west, south, east, north] = [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()];
    const latStep = gridStep(north - south);
    const lonStep = gridStep(east - west);
    const lats = gridValues(south, north, latStep);
    const lons = gridValues(west, east, lonStep);
    map.getSource('grid').setData({
      type: 'FeatureCollection',
      features: [
        ...lats.map((lat) => ({ type: 'Feature', geometry: { type: 'LineString', coordinates: [[west, lat], [east, lat]] } })),
        ...lons.map((lon) => ({ type: 'Feature', geometry: { type: 'LineString', coordinates: [[lon, south], [lon, north]] } })),
      ],
    });
    const centre = map.getCenter();
    const { clientWidth: width, clientHeight: height } = container;
    // Labels run along the top and left edges, so they also keep clear of any control in the
    // top-left corner (basemap.js's switcher).
    const corner = container.querySelector('.maplibregl-ctrl-top-left');
    const top = Math.max(LABEL_CLEARANCE.top, corner?.offsetHeight ?? 0);
    const left = Math.max(LABEL_CLEARANCE.left, corner?.offsetWidth ?? 0);
    labels.replaceChildren(
      ...lats.map((lat) => [lat, map.project([centre.lng, lat]).y])
        .filter(([, y]) => y > top && y < height - LABEL_CLEARANCE.bottom)
        .map(([lat, y]) => {
          const label = document.createElement('span');
          label.className = 'map-grid-label map-grid-latitude';
          label.textContent = gridLabel(lat, latStep, 'N', 'S');
          label.style.top = `${y}px`;
          return label;
        }),
      ...lons.map((lon) => [lon, map.project([lon, centre.lat]).x])
        .filter(([, x]) => x > left && x < width - LABEL_CLEARANCE.right)
        .map(([lon, x]) => {
          const label = document.createElement('span');
          label.className = 'map-grid-label map-grid-longitude';
          label.textContent = gridLabel(lon, lonStep, 'E', 'W');
          label.style.left = `${x}px`;
          return label;
        }),
    );
  };
  const markers = new Map(); // id -> { marker: maplibregl.Marker, spec, detail?, hovered, focused }

  // Marker Details: shown on hover and keyboard focus, or all at once while `detailsShown`.
  let detailsShown = false;
  const detailLimit = () => container.clientWidth - map.getPadding().right;
  const showDetail = (entry) => {
    if (!entry.detail) return;
    const front = entry.hovered || entry.focused;
    entry.detail.show(detailsShown || front, front);
    entry.detail.place(detailLimit());
  };
  const placeDetails = () => {
    const limit = detailLimit();
    for (const { detail } of markers.values()) detail?.place(limit);
  };
  // A tap is not a hover: on a touch screen the marker only opens what `choose` opens.
  const followHover = (entry, element) => {
    element.addEventListener('pointerenter', (event) => {
      if (event.pointerType === 'touch') return;
      entry.hovered = true;
      showDetail(entry);
    });
    element.addEventListener('pointerleave', () => {
      entry.hovered = false;
      showDetail(entry);
    });
    element.addEventListener('focus', () => {
      entry.focused = element.matches(':focus-visible'); // a click focuses it too
      showDetail(entry);
    });
    element.addEventListener('blur', () => {
      entry.focused = false;
      showDetail(entry);
    });
  };

  // Every marker's Trail as one line each, once the style has its source.
  const drawTrails = () => {
    if (!ready) return;
    const features = [];
    for (const { marker, spec } of markers.values()) {
      const points = spec.trail ?? [];
      const drawn = points.length >= 2;
      if (drawn) {
        features.push({
          type: 'Feature',
          geometry: { type: 'LineString', coordinates: points.map((point) => [point.longitude, point.latitude]) },
        });
      }
      marker.getElement().dataset.trail = String(drawn ? points.length : 0);
    }
    map.getSource('trails').setData({ type: 'FeatureCollection', features });
  };

  // Every style load, the first and each setBasemap's, starts with empty grid and trails sources.
  map.on('style.load', () => {
    ready = true;
    drawGrid();
    drawTrails();
  });
  map.on('move', drawGrid);
  map.on('move', placeDetails);
  // The basemap's pitch, north up, which the map rests at; null when it is already there.
  const unlevelled = () => {
    const pitch = basemap.pitch ?? 0;
    return map.getPitch() !== pitch || map.getBearing() !== 0 ? { pitch, bearing: 0 } : null;
  };
  // setBasemap eases the tilt, and any jump (follow's, on each new Position) cuts an ease short,
  // so whenever the map comes to rest it finishes at the basemap's pitch, north up.
  map.on('moveend', () => {
    const level = unlevelled();
    if (level) map.jumpTo(level);
  });

  new MutationObserver(() => {
    if (!ready) return;
    map.setPaintProperty('background', 'background-color', token(THEMED.background));
    if (basemap.grid) map.setPaintProperty('grid', 'line-color', token(THEMED.grid));
    map.setPaintProperty('trail', 'line-color', token(THEMED.trail));
  }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

  // Follow: on until the viewer pans by hand (a drag or the arrow keys), back on with recentre.
  // The recentre control shows only while follow is off.
  const recentreButton = document.createElement('button');
  recentreButton.type = 'button';
  recentreButton.className = 'map-recentre';
  recentreButton.textContent = 'Recentre';
  const recentreGroup = document.createElement('div');
  recentreGroup.className = 'maplibregl-ctrl maplibregl-ctrl-group';
  recentreGroup.hidden = true;
  recentreGroup.append(recentreButton);
  map.addControl({ onAdd: () => recentreGroup, onRemove: () => recentreGroup.remove() }, 'top-right');

  let following = true;
  const setFollowing = (value) => {
    following = value;
    recentreGroup.hidden = value;
  };
  map.on('dragstart', () => setFollowing(false));
  map.getCanvas().addEventListener('keydown', (event) => {
    if (event.key.startsWith('Arrow') && !event.shiftKey) setFollowing(false);
  });

  let placed = false; // whether the map has been fitted to the markers yet

  // Every marker in view at once, inside the padding: a lone marker centred at the current zoom,
  // several fitted with a margin, zoomed out as far as they need but never in past the starting
  // zoom. Nothing to fit into while the map is hidden or the padding leaves no room.
  const fitMarkers = () => {
    const specs = [...markers.values()].map((entry) => entry.spec);
    const inset = map.getPadding();
    const width = container.clientWidth - inset.left - inset.right;
    const height = container.clientHeight - inset.top - inset.bottom;
    if (!specs.length || width <= 0 || height <= 0) return;
    if (specs.length === 1) {
      map.jumpTo({ center: [specs[0].longitude, specs[0].latitude] });
      return;
    }
    const bounds = new maplibregl.LngLatBounds();
    for (const spec of specs) bounds.extend([spec.longitude, spec.latitude]);
    const padding = Math.min(FIT_PADDING, width / 4, height / 4);
    map.fitBounds(bounds, { padding, maxZoom: START_ZOOM, duration: 0 });
  };

  // Whether any marker's middle is outside the map, or under the padding, under any tilt.
  const outOfView = () => {
    const { clientWidth: width, clientHeight: height } = container;
    const inset = map.getPadding();
    return [...markers.values()].some(({ spec }) => {
      const { x, y } = map.project([spec.longitude, spec.latitude]);
      return x < inset.left || y < inset.top || x > width - inset.right || y > height - inset.bottom;
    });
  };

  const recentre = () => {
    setFollowing(true);
    fitMarkers();
  };
  recentreButton.addEventListener('click', recentre);

  return {
    setMarkers(specs) {
      let joined = false;
      const wanted = new Set(specs.map((spec) => spec.id));
      for (const [id, entry] of markers) {
        if (!wanted.has(id)) {
          entry.marker.remove();
          entry.detail?.marker.remove();
          markers.delete(id);
        }
      }
      for (const spec of specs) {
        let entry = markers.get(spec.id);
        if (!entry) {
          const { id } = spec;
          const marker = addMarker(map, spec, () => markers.get(id)?.spec.choose?.(marker.getElement()));
          entry = { marker, spec, hovered: false, focused: false };
          if (spec.detail) {
            entry.detail = addDetail(map, spec);
            followHover(entry, marker.getElement());
          }
          markers.set(spec.id, entry);
          joined = true;
        }
        entry.spec = spec;
        const element = entry.marker.getElement();
        entry.marker.setLngLat([spec.longitude, spec.latitude]);
        entry.marker.setRotation(spec.heading ?? 0);
        element.toggleAttribute('data-heading', spec.heading !== null);
        element.toggleAttribute('data-stale', Boolean(spec.stale));
        element.toggleAttribute('data-fixed', spec.source === 'fixed');
        if (spec.feedStatus) element.dataset.feedStatus = spec.feedStatus;
        else delete element.dataset.feedStatus;
        element.style.setProperty('--heading', `${spec.heading ?? 0}deg`); // keeps the glyph upright
        const parts = [`${spec.label}${spec.address ? `, ${spec.address}` : ''}: ${coordinates(spec)}`];
        if (spec.heading !== null) parts.push(`heading ${headingText(spec.heading)}`);
        if (spec.stale) parts.push(spec.since ? `last known ${spec.since} ago` : 'last known');
        if (spec.source === 'fixed') parts.push('Fixed Position');
        if (spec.feedStatus) parts.push(`Feed Status ${feedStatusLabel(spec.feedStatus)}`);
        element.setAttribute('aria-label', parts.join(', '));
        if (spec.description) element.setAttribute('aria-describedby', describedBy(spec.description));
        else element.removeAttribute('aria-describedby');
        entry.detail?.update(spec);
      }
      drawTrails();
      if (!placed || (following && (markers.size === 1 || joined || outOfView()))) fitMarkers();
      placed ||= markers.size > 0;
      for (const entry of markers.values()) showDetail(entry);
    },
    recentre,
    resize() {
      map.resize();
      if (following) fitMarkers();
      placeDetails();
    },
    // Show every Marker Detail (true) or only the hovered or focused one (false).
    showDetails(shown) {
      detailsShown = shown;
      for (const entry of markers.values()) showDetail(entry);
    },
    setPadding({ right = 0, bottom = 0 } = {}) {
      const { clientWidth: width, clientHeight: height } = container;
      const padding = { top: 0, left: 0, right, bottom };
      // The padding moves the map's centre to the middle of what it leaves, so pass the point
      // already there as the centre: the map doesn't jump under the viewer.
      const center = width && height ? map.unproject([(width - right) / 2, (height - bottom) / 2]) : map.getCenter();
      map.jumpTo({ padding, center });
      container.style.setProperty('--map-inset-right', `${right}px`);
      container.style.setProperty('--map-inset-bottom', `${bottom}px`);
      if (following && (markers.size === 1 || outOfView())) fitMarkers();
      placeDetails();
    },
    // Draw `next` (null for BLANK) under everything this module adds. A whole new style, not a
    // diff, so nothing of the old basemap's sources or layers lingers; the DOM markers, the view
    // and follow are the map's, not the style's, so they stay.
    setBasemap(next) {
      basemap = next ?? BLANK;
      addProtocols(basemap);
      ready = false;
      labels.hidden = !basemap.grid;
      labels.replaceChildren();
      map.setStyle(style(basemap), { diff: false });
      const level = unlevelled();
      if (level) map.easeTo(level);
    },
    // Add `element` as a control in a corner of the map ('top-left', 'top-right', …).
    addControl(element, corner) {
      map.addControl({ onAdd: () => element, onRemove: () => element.remove() }, corner);
    },
  };
}
