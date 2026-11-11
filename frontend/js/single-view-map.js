// Single View's map: the selected Camera's Device on a map below the stage (map.js). It reads
// `positions`, `cameras`, `selected` and `view`, and shows only while that Device has a
// Position. Its collapse toggle sets `mapCollapsed`, which view-state.js keeps per browser.
//
// The marker carries the Device's Trail (trails.js), kept while the map is open on that Device,
// so each time the map opens on a Device it fetches the Trail so far afresh. A Device showing its
// Fixed Position draws none (trails.js).
//
// MapLibre loads the first time an expanded map has something to show (map-loader.js), so a
// browser that only ever uses Multi View, or keeps the map collapsed, never fetches it.

import { lazyMap } from './map-loader.js';
import { positionOf } from './position.js';
import { createTrails } from './trails.js';
import { selectedCamera } from './view-state.js';

export function mountMapOnSingleView(root, store) {
  const section = document.createElement('section');
  section.className = 'single-view-map';
  section.setAttribute('aria-labelledby', 'map-heading');
  section.hidden = true;
  const heading = document.createElement('h3');
  heading.id = 'map-heading';
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'panel-toggle map-toggle'; // the Panels' chevron toggle
  toggle.textContent = 'Map';
  toggle.setAttribute('aria-controls', 'map-body');
  heading.append(toggle);
  const body = document.createElement('div');
  body.className = 'map';
  body.id = 'map-body';
  section.append(heading, body);
  root.querySelector('.stage').after(section);
  toggle.addEventListener('click', () => store.set('mapCollapsed', !store.get('mapCollapsed')));

  const lazy = lazyMap(body, store, { title: 'Map, drag or use the arrow keys to pan', onReady: () => update() });
  const trails = createTrails(() => update());

  let shownDevice = null;
  const update = () => {
    const camera = selectedCamera(store);
    const position = store.get('view') === 'single' ? positionOf(store, camera) : null;
    const collapsed = store.get('mapCollapsed') === true;
    section.hidden = !position;
    toggle.setAttribute('aria-expanded', String(!collapsed));
    body.hidden = collapsed;
    const open = Boolean(position) && !collapsed;
    if (!open) {
      lazy.hide();
      trails.keep([]); // fetched afresh when it opens again
      return;
    }
    trails.keep([camera.device], store.get('positions'));
    trails.extend(store.get('positions'));
    const map = lazy.show();
    if (!map) return;
    map.setMarkers([{ id: camera.device, label: camera.device, ...position, trail: trails.points(camera.device) }]);
    if (camera.device !== shownDevice) map.recentre(); // another Device: start by following it
    shownDevice = camera.device;
  };

  for (const key of ['positions', 'cameras', 'selected', 'view', 'mapCollapsed', 'settings']) store.on(key, update);
}
