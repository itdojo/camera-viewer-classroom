// Multi View: the Cameras this browser put on screen, one tile each in the
// Camera list's order (view-state.js), in a grid sized to the selection: 1,
// 2, 4 (2×2), 6 (3×2) or 9 (3×3) tiles. A selection between those sizes takes the next one up and leaves cells
// empty. Tiles play only while Multi View is showing; Single View gets the
// browser's whole attention.
//
// On a wide or landscape screen the grid fits the window's height (app.css).
// The masthead wraps to a second line on a narrow window, so its height is
// measured here as --masthead rather than guessed.

import { createTile } from './tile.js';
import { openSingleView, orderedCameras } from './view-state.js';

// A phone on its side: wide and short, so a few Cameras sit in one row.
export const SHORT_LANDSCAPE = '(orientation: landscape) and (max-height: 540px)';

// [columns, rows] for each selection size up to nine. On a short landscape
// screen up to four Cameras share one row, since a second row would leave
// each video a sliver.
export function gridFor(count, { short = false } = {}) {
  if (short && count <= 4) return [Math.max(count, 1), 1];
  if (count <= 1) return [1, 1];
  if (count === 2) return [2, 1];
  if (count <= 4) return [2, 2];
  if (count <= 6) return [3, 2];
  return [3, 3];
}

export function mountMultiView(root, store) {
  const grid = root.querySelector('[data-grid]');
  const empty = root.querySelector('[data-empty]');
  const tiles = new Map(); // Camera id -> tile
  const shortLandscape = window.matchMedia(SHORT_LANDSCAPE);
  const masthead = document.querySelector('.masthead');
  if (masthead) {
    new ResizeObserver(() => {
      document.documentElement.style.setProperty('--masthead', `${masthead.offsetHeight}px`);
    }).observe(masthead);
  }

  const render = () => {
    const showing = store.get('view') === 'multi';
    root.hidden = !showing;
    const cameras = new Map((store.get('cameras') ?? []).map((camera) => [camera.id, camera]));
    const onScreen = new Set(store.get('onScreen') ?? []);
    const wanted = showing ? orderedCameras(store).map((camera) => camera.id).filter((id) => onScreen.has(id)) : [];

    for (const [id, tile] of tiles) {
      if (!wanted.includes(id)) {
        tile.close();
        tiles.delete(id);
      }
    }
    for (const id of wanted) {
      const camera = cameras.get(id);
      if (tiles.has(id)) tiles.get(id).update(camera);
      else tiles.set(id, createTile(camera, { store, onOpen: (cameraId) => openSingleView(store, cameraId) }));
    }
    const order = wanted.map((id) => tiles.get(id).element);
    if (order.some((element, n) => grid.children[n] !== element) || grid.children.length !== order.length) {
      grid.replaceChildren(...order); // synchronous, so moved videos keep playing
    }

    const [columns, rows] = gridFor(wanted.length, { short: shortLandscape.matches });
    root.style.setProperty('--columns', columns);
    root.style.setProperty('--rows', rows);
    empty.hidden = !showing || wanted.length > 0;
  };

  store.on('view', render);
  store.on('onScreen', render);
  store.on('cameras', render);
  store.on('cameraOrder', render);
  shortLandscape.addEventListener('change', render);
  window.addEventListener('pagehide', () => {
    for (const tile of tiles.values()) tile.close();
    tiles.clear();
  });
}
