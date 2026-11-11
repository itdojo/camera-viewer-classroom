// Positions: where each Device is, from the `positions` store key (the server's map of Device
// label to Position). A Camera has its Device's Position, so every Camera on one Device shows
// the same one. Units arrive as the server sends them (metres, m/s, degrees true); this module
// owns how they read. The formatters, and `ageNow`, are exported for the tiles and the maps.
//
// `age` is seconds at the moment the server sent the Position, and the server sends again only
// on a new fix or when the Position goes stale, so a stale Position's age is counted on here.
// A Fixed Position (`source: "fixed"`, entered by hand in the Devices Panel) has coordinates only.

import { decorateTiles } from './tile.js';
import { selectedCamera } from './view-state.js';

// 51.500012 -> "51.50001", 5 decimals (about 1 m).
export function coordinates(position) {
  return `${position.latitude.toFixed(5)}, ${position.longitude.toFixed(5)}`;
}

// 5.0 m/s -> "18.0 km/h"
export function speed(metresPerSecond) {
  return `${(metresPerSecond * 3.6).toFixed(1)} km/h`;
}

// 46.6 -> "47°", 359.7 -> "0°"
export function heading(degrees) {
  return `${Math.round(degrees) % 360}°`;
}

// 12 -> "12 s", 125 -> "2 min", 7300 -> "2 h"
export function age(seconds) {
  if (seconds < 60) return `${Math.floor(seconds)} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
  return `${Math.floor(seconds / 3600)} h`;
}

// "2026-10-15T09:30:05.000Z" -> "2026-10-15 09:30:05 UTC"
function gpsTime(text) {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/.exec(text ?? '');
  return match ? `${match[1]} ${match[2]} UTC` : null;
}

// Altitude, satellites, HDOP and GPS time, for whoever needs to judge the fix.
export function details(position) {
  const parts = [];
  if (position.altitude !== null) parts.push(`Altitude ${Math.round(position.altitude)} m`);
  if (position.satellites !== null) parts.push(`${position.satellites} satellite${position.satellites === 1 ? '' : 's'}`);
  if (position.hdop !== null) parts.push(`HDOP ${position.hdop.toFixed(1)}`);
  const time = gpsTime(position.gps_time);
  if (time) parts.push(`GPS time ${time}`);
  return parts.join(' · ');
}

// The Position of a Camera's Device, or null when it has none.
export function positionOf(store, camera) {
  return camera ? store.get('positions')?.[camera.device] ?? null : null;
}

let detailIds = 0;

// The header line: coordinates (their tooltip holds the details), speed and heading, and the
// age once stale, or "Fixed Position" for one entered by hand; `data-fix` says which of live,
// stale, fixed or none it is. `update(position, receivedAt)` redraws it; null hides it, unless
// `none` is given, which is then the line's text (the Map Preview's "No Position").
// `coordinatesElement` is the coordinates' node, for whoever selects or copies them.
export function positionLine({ none = null } = {}) {
  const element = document.createElement('div');
  element.className = 'position';
  element.setAttribute('role', 'group');
  element.setAttribute('aria-label', 'Position');
  element.hidden = true;
  const where = document.createElement('span');
  where.className = 'position-coordinates';
  where.tabIndex = 0; // so the tooltip opens from the keyboard too
  const tooltip = document.createElement('span');
  tooltip.className = 'position-detail';
  tooltip.setAttribute('role', 'tooltip');
  tooltip.id = `position-detail-${++detailIds}`;
  where.setAttribute('aria-describedby', tooltip.id);
  const motion = document.createElement('span');
  motion.className = 'position-motion';
  const ageText = document.createElement('span');
  ageText.className = 'position-age';
  const source = document.createElement('span');
  source.className = 'position-source';
  source.textContent = 'Fixed Position';
  const absent = document.createElement('span');
  absent.className = 'position-none';
  absent.textContent = none ?? '';
  element.append(where, tooltip, motion, ageText, source, absent);

  let shown = null;
  let receivedAt = 0;

  const drawAge = () => {
    ageText.hidden = !shown?.stale;
    if (shown?.stale) ageText.textContent = `last fix ${ageNow(shown, receivedAt)} ago`;
  };
  setInterval(drawAge, 1000);

  return {
    element,
    coordinatesElement: where,
    update(position, at) {
      shown = position;
      receivedAt = at;
      element.hidden = !position && none === null;
      element.dataset.fix = fixOf(position);
      for (const part of [where, tooltip, motion]) part.hidden = !position;
      absent.hidden = Boolean(position);
      if (!position) {
        element.removeAttribute('data-stale');
        element.removeAttribute('data-fixed');
        source.hidden = true;
        ageText.hidden = true;
        for (const part of [where, tooltip, motion, ageText]) part.textContent = '';
        return;
      }
      const isFixed = position.source === 'fixed';
      element.toggleAttribute('data-stale', position.stale);
      element.toggleAttribute('data-fixed', isFixed);
      source.hidden = !isFixed;
      where.textContent = coordinates(position);
      tooltip.textContent = isFixed ? 'Entered by hand in the Devices Panel' : details(position);
      motion.textContent = [
        position.speed !== null ? speed(position.speed) : null,
        position.heading !== null ? heading(position.heading) : null,
      ].filter(Boolean).join(' · ');
      drawAge();
    },
  };
}

// How far a Position can be trusted, as a tile's fix-status indicator says it: the short text
// on the tile and the longer title, where `since` is a stale fix's age ("2 min"). A new state
// is an entry here, a case in fixOf and a colour in app.css (`.tile-fix[data-fix=...]`).
const FIX = {
  live: { text: () => 'GPS', title: () => 'Live GPS fix' },
  stale: { text: (since) => `last fix ${since} ago`, title: (since) => `Last GPS fix ${since} ago` },
  fixed: { text: () => 'Fixed', title: () => 'Fixed Position, entered by hand' },
  none: { text: () => 'No Position', title: () => 'No Position' },
};

function fixOf(position) {
  if (!position) return 'none';
  if (position.source === 'fixed') return 'fixed';
  return position.stale ? 'stale' : 'live';
}

// A stale Position's age now: its age when sent, plus the time since this browser received it.
export function ageNow(position, receivedAt) {
  return age(position.age + (Date.now() - receivedAt) / 1000);
}

// A tile's Position: the fix-status indicator, then compact coordinates (no speed or heading,
// which Single View has). `update(position, receivedAt)` redraws it.
function tilePosition() {
  const element = document.createElement('span');
  element.className = 'tile-position-group';
  element.setAttribute('role', 'group');
  element.setAttribute('aria-label', 'Position');
  const fix = document.createElement('span');
  fix.className = 'tile-fix';
  const where = document.createElement('span');
  where.className = 'tile-coordinates';
  element.append(fix, where);

  return {
    element,
    update(position, receivedAt) {
      const state = fixOf(position);
      const since = position?.stale ? ageNow(position, receivedAt) : null;
      element.toggleAttribute('data-stale', state === 'stale');
      fix.dataset.fix = state;
      fix.textContent = FIX[state].text(since);
      fix.title = FIX[state].title(since);
      where.textContent = position ? coordinates(position) : '';
    },
  };
}

// Multi View: every tile's Position, in its `position` slot. Tiles redraw on `cameras`
// (tile.js) and on `positions` here; a tile no longer on the page is dropped at the next one.
export function mountPositionOnTiles(store) {
  const tiles = new Map(); // tile element -> { control, camera }
  let receivedAt = Date.now();
  const draw = ({ control, camera }) => control.update(positionOf(store, camera), receivedAt);

  decorateTiles(({ slots, camera, element }) => {
    if (!tiles.has(element)) tiles.set(element, { control: tilePosition(), camera });
    const tile = tiles.get(element);
    tile.camera = camera;
    if (!slots.position.contains(tile.control.element)) slots.position.replaceChildren(tile.control.element);
    draw(tile);
  });
  const drawAll = () => {
    for (const [element, tile] of tiles) {
      if (element.isConnected) draw(tile);
      else tiles.delete(element);
    }
  };
  store.on('positions', () => {
    receivedAt = Date.now();
    drawAll();
  });
  setInterval(drawAll, 1000); // a stale age counts on between events
}

// Single View: the selected Camera's Position, in the header after the Feed's description.
export function mountPositionOnSingleView(root, store) {
  const line = positionLine();
  root.querySelector('[data-meta]').after(line.element);
  let receivedAt = Date.now();
  const update = () => {
    line.update(positionOf(store, selectedCamera(store)), receivedAt);
  };
  store.on('positions', () => {
    receivedAt = Date.now();
    update();
  });
  store.on('cameras', update);
  store.on('selected', update);
}
