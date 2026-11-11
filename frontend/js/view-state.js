// This browser's view state, which belongs to no one else: which view is
// showing (`view`: 'multi', 'single' or 'map'), the View Single View was
// opened from and goes back to (`singleFrom`: 'multi' or 'map'), the Camera in
// Single View (`selected`), and the Cameras on screen in Multi View (`onScreen`, an
// ordered list of Camera ids, at most MAX_ON_SCREEN), whether Single View's map is
// collapsed (`mapCollapsed`), the order of the Camera and Device lists
// (`cameraOrder`, Camera ids; `deviceOrder`, Device labels), and the Basemap
// every map draws (`basemap`, an id from basemap.js, or null until the viewer
// chooses one; basemap.js falls back when the instance doesn't offer it), and
// whether it draws 3D buildings where the Basemap has them (`buildings3d`), and whether Map
// View shows every Marker Detail at once (`markerDetails`). Multi View's
// tiles follow the Camera list's order. An empty order is the instance's own,
// and a Camera or Device the saved order does not name follows the named ones
// in the instance's order.
//
// The state lives in the store like everything else, and is mirrored to
// localStorage so a reload keeps it. Storage can be missing or throw (private
// windows, blocked site data); the UI then simply starts fresh.

import { escapeIsFree } from './dom.js';

const STORAGE_KEY = 'camera-viewer:view';
const KEYS = ['view', 'singleFrom', 'selected', 'onScreen', 'mapCollapsed', 'cameraOrder', 'deviceOrder', 'basemap', 'buildings3d', 'markerDetails'];

export const MAX_ON_SCREEN = 9;

const ids = (saved) => (Array.isArray(saved) ? saved.filter((id) => typeof id === 'string') : []);

// The View Single View goes back to, from any `view` or saved value: Map View or else Multi View.
const backTarget = (view) => (view === 'map' ? 'map' : 'multi');

function load() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}');
    return {
      view: ['single', 'map'].includes(saved.view) ? saved.view : 'multi',
      singleFrom: backTarget(saved.singleFrom),
      selected: typeof saved.selected === 'string' ? saved.selected : null,
      onScreen: ids(saved.onScreen).slice(0, MAX_ON_SCREEN),
      mapCollapsed: saved.mapCollapsed === true,
      cameraOrder: ids(saved.cameraOrder),
      deviceOrder: ids(saved.deviceOrder),
      basemap: typeof saved.basemap === 'string' ? saved.basemap : null,
      buildings3d: saved.buildings3d === true,
      markerDetails: saved.markerDetails === true,
    };
  } catch {
    return { view: 'multi', singleFrom: 'multi', selected: null, onScreen: [], mapCollapsed: false, cameraOrder: [], deviceOrder: [], basemap: null, buildings3d: false, markerDetails: false };
  }
}

function save(store) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(Object.fromEntries(KEYS.map((k) => [k, store.get(k)]))));
  } catch {
    // storage unavailable: the state still works for this page's lifetime
  }
}

export function isOnScreen(store, cameraId) {
  return (store.get('onScreen') ?? []).includes(cameraId);
}

// Put a Camera on screen or take it off. Adding beyond MAX_ON_SCREEN is
// refused. Changing the selection from Single View shows Multi View, which
// shows what it did; Map View stays, since its viewer chose the map.
export function setOnScreen(store, cameraId, wanted) {
  const current = store.get('onScreen') ?? [];
  if (wanted === current.includes(cameraId)) return;
  if (wanted && current.length >= MAX_ON_SCREEN) return;
  store.set('onScreen', wanted ? [...current, cameraId] : current.filter((id) => id !== cameraId));
  if (store.get('view') !== 'map') store.set('view', 'multi');
}

// items sorted by a saved order of their ids; those it does not name keep
// their own order after the named ones.
export function inOrder(items, order, idOf) {
  const rank = new Map(order.map((id, index) => [id, index]));
  const of = (item) => rank.get(idOf(item)) ?? order.length;
  return [...items].sort((a, b) => of(a) - of(b));
}

// Every Camera, in this browser's order.
export function orderedCameras(store) {
  return inOrder(store.get('cameras') ?? [], store.get('cameraOrder') ?? [], (camera) => camera.id);
}

// The Camera in Single View, from the `cameras` list; undefined if there is none or it has gone.
export function selectedCamera(store) {
  return (store.get('cameras') ?? []).find((c) => c.id === store.get('selected'));
}

// Open a Camera in Single View, remembering the View it was opened from (a
// tile or the Camera list in Multi View, a marker in Map View), so back and
// Esc return there. Switching Cameras inside Single View keeps that View.
export function openSingleView(store, cameraId) {
  const from = store.get('view');
  if (from !== 'single') store.set('singleFrom', backTarget(from));
  store.set('selected', cameraId);
  store.set('view', 'single');
}

// Back from Single View, to the View it was opened from.
export function leaveSingleView(store) {
  store.set('view', backTarget(store.get('singleFrom')));
}

// Load the saved state into the store, keep saving it, and wire Esc. Call
// before mounting the views so they start from the saved state.
export function mountViewState(store) {
  const saved = load();
  for (const key of KEYS) store.set(key, saved[key]);
  for (const key of KEYS) store.on(key, () => save(store));

  // A Device removed by hand takes its Cameras off screen. Offline Devices
  // stay listed, so their Cameras stay on screen.
  store.on('cameras', (cameras) => {
    const known = new Set(cameras.map((camera) => camera.id));
    const onScreen = store.get('onScreen') ?? [];
    if (onScreen.some((id) => !known.has(id))) store.set('onScreen', onScreen.filter((id) => known.has(id)));
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || store.get('view') !== 'single' || !escapeIsFree(event)) return;
    event.preventDefault(); // spent, so a Map Preview reopened by the return keeps it
    leaveSingleView(store);
  });
}
