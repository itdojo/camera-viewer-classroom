// The Camera list: every Camera on the instance, each a card shaped like a
// Device entry. Its first line is the Device label, the Camera's Feed Status
// and a Connect toggle, a checkbox that puts the Camera on screen in Multi View
// (at most MAX_ON_SCREEN); its second line is the Camera name and the Device's
// address. The card is a button that opens the Camera in Single View, named
// "<camera> <device> <status>" like the tiles, so it reads the same as before.
// Renders from the store's `cameras` and `devices` (server state) and this
// browser's `selected`, `onScreen` and `view`. Items are kept by Camera id and
// updated in place, so Connect keeps focus when the selection it changed
// re-renders the list. A Camera with a Recording carries its recording badge,
// so background Recordings stay visible.
//
// The list is in this browser's order (`cameraOrder`, view-state.js), which
// Multi View's tiles follow too. Alt+↑/↓ on a Camera's button or grip, or
// dragging the grip, moves it (reorder.js).

import { feedStatusBadge, setFeedStatus } from './feed-status.js';
import { recordingBadge } from './recording.js';
import { moved, placeInOrder, reorderable } from './reorder.js';
import { isOnScreen, MAX_ON_SCREEN, openSingleView, orderedCameras, setOnScreen } from './view-state.js';

const KEYS = 'Alt+ArrowUp Alt+ArrowDown';
let itemIds = 0;

function cameraItem(camera, store) {
  const id = `camera-item-${++itemIds}`;
  const span = (className, text) => {
    const node = document.createElement('span');
    node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  // The headline: the Device label and the Feed Status, inside the button,
  // whose ::after stretches over the whole card so either line opens it.
  const device = span('camera-device');
  device.id = `${id}-device`;
  const status = feedStatusBadge(camera.feed_status, camera.feed_status_reason);
  status.id = `${id}-status`;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'camera-open';
  button.setAttribute('aria-keyshortcuts', KEYS);
  button.append(device, status);
  button.addEventListener('click', () => openSingleView(store, item.dataset.camera));

  // The second line: what the Camera is and where its Device lives.
  const name = span('camera-name');
  name.id = `${id}-name`;
  const address = span('camera-address');
  const recording = span('camera-recording');
  recording.id = `${id}-recording`;
  const meta = document.createElement('p');
  meta.className = 'meta camera-meta';
  meta.append(name, address, recording);
  button.setAttribute('aria-labelledby', `${name.id} ${device.id} ${status.id} ${recording.id}`);

  // Connect: a real checkbox laid over a button-shaped label, so it keeps its
  // checkbox role and state while reading like the Devices' Remove.
  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.className = 'on-screen';
  checkbox.addEventListener('change', () => {
    setOnScreen(store, item.dataset.camera, checkbox.checked);
    checkbox.checked = isOnScreen(store, item.dataset.camera); // a refused add unticks
  });
  const connect = document.createElement('label');
  connect.className = 'connect';
  connect.append(checkbox, span('connect-text', 'Connect'));

  // Named "Move" alone, with the Camera as its description, so a locator
  // for a Camera's button by its name never also finds its grip.
  const grip = document.createElement('button');
  grip.type = 'button';
  grip.className = 'item-grip';
  grip.setAttribute('aria-label', 'Move');
  grip.setAttribute('aria-describedby', `${name.id} ${device.id}`);
  grip.setAttribute('aria-keyshortcuts', KEYS);
  grip.title = 'Drag, or press Alt+↑/↓, to move this Camera';
  const dots = document.createElement('span');
  dots.setAttribute('aria-hidden', 'true');
  dots.textContent = '⠿';
  grip.append(dots);

  const head = document.createElement('div');
  head.className = 'camera-head';
  head.append(grip, button, connect);
  const item = document.createElement('li');
  item.dataset.camera = camera.id;
  item.append(head, meta);

  item.update = (next) => {
    name.textContent = next.name;
    device.textContent = next.device;
    const host = (store.get('devices') ?? []).find((d) => d.label === next.device)?.address;
    address.textContent = host ? ` · ${host}` : '';
    setFeedStatus(status, next.feed_status, next.feed_status_reason);
    recording.replaceChildren(...[recordingBadge(next.recording)].filter(Boolean));
    checkbox.setAttribute('aria-label', `Connect ${next.name} · ${next.device} to Multi View`);
    const onScreen = isOnScreen(store, next.id);
    const full = (store.get('onScreen') ?? []).length >= MAX_ON_SCREEN;
    checkbox.checked = onScreen;
    checkbox.disabled = !onScreen && full;
    connect.title = checkbox.disabled ? `Multi View holds at most ${MAX_ON_SCREEN} Cameras`
      : onScreen ? 'On screen in Multi View' : 'Show in Multi View';
    button.setAttribute('aria-pressed', String(store.get('view') === 'single' && next.id === store.get('selected')));
  };
  return item;
}

export function mountCameraList(root, store, { announce }) {
  const list = root.querySelector('.camera-list');
  const empty = root.querySelector('[data-empty]');
  const items = new Map(); // Camera id -> <li>

  const render = () => {
    const cameras = orderedCameras(store);
    const ids = new Set(cameras.map((camera) => camera.id));
    for (const [id, item] of items) {
      if (!ids.has(id)) {
        item.remove();
        items.delete(id);
      }
    }
    for (const camera of cameras) {
      if (!items.has(camera.id)) items.set(camera.id, cameraItem(camera, store));
      items.get(camera.id).update(camera);
    }
    placeInOrder(list, cameras.map((camera) => items.get(camera.id)));
    empty.hidden = cameras.length > 0;
  };
  for (const key of ['cameras', 'devices', 'selected', 'onScreen', 'view', 'cameraOrder']) store.on(key, render);

  reorderable(list, {
    item: 'li', grip: '.item-grip', keys: '.item-grip, .camera-open',
    idOf: (item) => item.dataset.camera,
    move: (id, index) => {
      const cameras = orderedCameras(store);
      const order = moved(cameras.map((camera) => camera.id), id, index);
      if (!order) return;
      store.set('cameraOrder', order);
      const camera = cameras.find((c) => c.id === id);
      announce(`${camera.name} · ${camera.device} moved to position ${order.indexOf(id) + 1} of ${order.length}`);
    },
  });
}
