// Map loading, shared by every map in the UI. MapLibre (about 1.1 MB) comes in with map.js,
// the only module that imports it (ADR 0011), and map.js is imported here lazily, the first time
// a map is opened, so a browser that only ever uses Multi View never fetches it. The Basemap is
// this browser's choice of those the `settings` event's `map` field offers (basemap.js), loaded
// alongside it, and every map opened here gets the Basemap switcher and follows the choice.
//
// `lazyMap` is the life of a map that shows and hides with its View: opened the first time it
// is shown once `settings` has arrived, and resized each time it shows again, since a hidden map
// has no size.

import { chosenBasemap, mountBasemapSwitcher } from './basemap.js';

let loading = null;
const loadMapModule = () => (loading ??= import('./map.js'));

// A new map in `container`, as map.js's `createMap` handle, once MapLibre and the Basemap have
// loaded. `settings` must have arrived, since it names the Basemaps.
export function openMap(container, store, { title }) {
  return Promise.all([loadMapModule(), chosenBasemap(store)]).then(([{ createMap }, { id, basemap }]) => {
    const map = createMap(container, { basemap, title });
    mountBasemapSwitcher(store, map, id);
    return map;
  });
}

// A map in `container` that its caller shows and hides. `show()` returns the map's handle, resized
// if it was hidden, or null while it is still opening; the first call opens it, once `settings`
// is in the store, then runs `onOpen(handle)` and `onReady()`, so the caller can draw. `hide()`
// says it was hidden, and `handle()` is the map or null, for a caller that only needs it if open.
export function lazyMap(container, store, { title, onOpen = () => {}, onReady }) {
  let map = null;
  let opening = null;
  let shown = false;
  return {
    show() {
      if (!map) {
        if (store.get('settings')) { // the Basemap is named there; `settings` comes first
          opening ??= openMap(container, store, { title }).then((created) => {
            map = created;
            onOpen(map);
            onReady();
          });
        }
        return null;
      }
      if (!shown) map.resize(); // it had no size while hidden
      shown = true;
      return map;
    },
    hide() {
      shown = false;
    },
    handle: () => map,
  };
}
