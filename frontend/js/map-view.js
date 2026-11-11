// Map View: one map filling the viewing area (map.js), with one marker for every Device that
// has a Position, labelled with the Device, while this browser's view is 'map'. The map fits
// every marker and keeps refitting as they move and Devices come and go, until the viewer drags
// it; Recentre fits them all again (map.js's follow).
//
// Choosing a marker, by click, Enter or Space, opens the Map Preview on its Device
// (map-preview.js): a panel beside the map playing one of the Device's Cameras. The map pads
// around the panel, so follow and Recentre fit the markers into the part it leaves.
//
// Each marker shows the worst Feed Status among its Device's Cameras, and a stale one counts its
// age on once a second, since a stale Position isn't sent again (map.js draws both). Devices with
// no Position are listed over the map ("No Position: cam4, cam7"), in this browser's Device order;
// the list hides while every Device has one.
//
// Each marker has a Marker Detail (map.js): a card beside it with its Device's label, address and
// Position, shown while hovered or focused. The "Details" control beside Recentre shows every
// marker's at once; it is this browser's view-state key `markerDetails`, kept across reloads, and
// opens nothing. The address joins each marker's name too.
//
// Each marker carries its Device's Trail (trails.js, shared with Single View's map), kept while
// Map View shows, so each time it opens it fetches every Trail so far afresh. A Device showing
// its Fixed Position draws none (trails.js).
//
// MapLibre loads the first time Map View shows (map-loader.js), so a browser that only ever uses
// Multi View never fetches it.

import { worstFeedStatus } from './feed-status.js';
import { lazyMap } from './map-loader.js';
import { mountMapPreview } from './map-preview.js';
import { ageNow } from './position.js';
import { createTrails } from './trails.js';
import { inOrder, orderedCameras } from './view-state.js';

export function mountMapView(root, store) {
  const body = root.querySelector('.map');
  const unplaced = document.createElement('p');
  unplaced.className = 'maplibregl-ctrl map-no-position';
  unplaced.setAttribute('role', 'status');
  unplaced.hidden = true;
  const details = document.createElement('button');
  details.type = 'button';
  details.className = 'map-details';
  details.textContent = 'Details';
  details.setAttribute('aria-label', 'Show all Marker Details');
  details.title = 'Show all Marker Details';
  details.addEventListener('click', () => store.set('markerDetails', !store.get('markerDetails')));
  const detailsGroup = document.createElement('div');
  detailsGroup.className = 'maplibregl-ctrl maplibregl-ctrl-group';
  detailsGroup.append(details);
  const showDetails = () => {
    const shown = Boolean(store.get('markerDetails'));
    details.setAttribute('aria-pressed', String(shown));
    lazy.handle()?.showDetails(shown);
  };

  const lazy = lazyMap(body, store, {
    title: 'Map, drag or use the arrow keys to pan',
    onOpen: (map) => {
      map.addControl(unplaced, 'top-left');
      map.addControl(detailsGroup, 'top-right');
    },
    onReady: () => {
      showDetails();
      update();
    },
  });
  const trails = createTrails(() => update());
  const preview = mountMapPreview(root, store, { onInsets: (insets) => lazy.handle()?.setPadding(insets) });

  let receivedAt = Date.now();
  store.on('positions', () => {
    receivedAt = Date.now();
  });

  // What a marker shows beyond its Position: its Cameras' worst Feed Status, and a stale age.
  const markerState = (position, cameras) => ({
    feedStatus: worstFeedStatus(cameras.map((camera) => camera.feed_status)),
    since: position.stale ? ageNow(position, receivedAt) : null,
  });

  const listUnplaced = (positions) => {
    const devices = inOrder(store.get('devices') ?? [], store.get('deviceOrder') ?? [], (device) => device.label);
    const labels = devices.map((device) => device.label).filter((label) => !(label in positions));
    const text = labels.length ? `No Position: ${labels.join(', ')}` : '';
    if (unplaced.textContent !== text) unplaced.replaceChildren(...unplacedNames(labels));
    unplaced.hidden = !labels.length;
  };

  // "No Position: cam4, cam7", each name a button that opens the Map Preview on its Device.
  // Rebuilt only when the list changes, so a focused name keeps its focus.
  const unplacedNames = (labels) => {
    if (!labels.length) return [];
    const names = labels.flatMap((label, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'map-no-position-name';
      button.textContent = label;
      button.addEventListener('click', () => preview.open(label, button));
      return index ? [', ', button] : [button];
    });
    return ['No Position: ', ...names];
  };

  const update = () => {
    const showing = store.get('view') === 'map';
    root.hidden = !showing;
    if (!showing) {
      lazy.hide();
      trails.keep([]); // fetched afresh when it shows again
      return;
    }
    const positions = store.get('positions') ?? {};
    trails.keep(Object.keys(positions), positions);
    trails.extend(positions);
    const map = lazy.show();
    if (!map) return;

    const cameras = orderedCameras(store);
    const addresses = new Map((store.get('devices') ?? []).map((device) => [device.label, device.address]));
    map.setMarkers(Object.entries(positions).map(([device, position]) => {
      const onDevice = cameras.filter((camera) => camera.device === device);
      return {
        id: device,
        label: device,
        address: addresses.get(device),
        ...position,
        ...markerState(position, onDevice),
        trail: trails.points(device),
        choose: (marker) => preview.open(device, marker),
        description: 'Opens the Map Preview',
        detail: true,
      };
    }));
    listUnplaced(positions);
  };

  store.on('markerDetails', showDetails);
  for (const key of ['view', 'positions', 'cameras', 'cameraOrder', 'devices', 'deviceOrder', 'settings']) {
    store.on(key, update);
  }
  setInterval(() => { // a stale marker's age counts on between events
    if (lazy.handle() && store.get('view') === 'map' && Object.values(store.get('positions') ?? {}).some((p) => p.stale)) update();
  }, 1000);
}
