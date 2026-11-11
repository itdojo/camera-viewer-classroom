// The Map Preview: a panel docked on Map View's map that plays one of a Device's Cameras beside
// it, so the map stays live around the picture. Choosing a marker opens it on that Device and
// plays the Device's first Camera in this browser's Camera order at once; choosing another
// marker swaps it to that Device, closing the old WHEP session before the new one opens, so the
// browser never holds two. The panel names the Camera and Device and carries the Camera's Feed
// Status, the Stall overlay, and the Unencrypted and recording badges, as a tile does. A Device
// with no Cameras opens it with "No Cameras" and no video area. A Feed that never starts shows
// keepPlaying's "Feed unavailable… Retrying…" and its Feed Status, as in Single View. Its corner
// button, or turning a phone to landscape, puts the Feed Full Screen (full-screen.js).
//
// It closes on its close button, on Esc (an open Management Bar takes the first Esc, and a
// dialog such as Full Screen keeps its own), on leaving Map View and when its Device is removed;
// a click on the map leaves it open. It is never saved in view state.
//
// Opening it pushes one history entry, marked in `history.state`, so Back closes it; swapping
// reuses that entry, and closing any other way pops it. It closes on a popstate only once its
// marker has gone from `history.state`, so another history user stacked over it (Full Screen)
// can pop its own entry and leave the panel open.
//
// "Open in Single View" opens the playing Camera there. Every way back to Map View from that
// Single View (its back button, Esc, the masthead switch) reopens the panel on the Camera Single
// View shows by then, previewing that Camera's Device. That return is remembered in memory only,
// so a reload comes back to Map View with no panel and no Feed.
//
// It is a non-modal region named "Map Preview: <Device> · <Camera>" after the Camera playing.
// Opening it moves focus to its close button, and closing it returns focus to whatever opened it
// (a marker, or a name in the No Position list), unless the viewer has moved focus elsewhere.
//
// It lies over the map, so it reports the strip of map it covers through `onInsets({ right,
// bottom })` whenever that changes (0 when closed): docked on the right edge it covers a strip
// on the right, and a panel wider than half the map covers one along the bottom: the sheet
// fixed along a phone's screen, whose share of the map changes as the page scrolls. Map View
// passes that to map.js's padding, so follow and Recentre fit the markers into the rest.
//
// Below the screen come three `data-slot` parts, each hidden while empty: the Device's Position,
// its Cameras as choices, and the actions (Open in Single View).

import { element, escapeIsFree } from './dom.js';
import { setFeedStatus } from './feed-status.js';
import { unencryptedBadge } from './encryption.js';
import { mountFullScreenOnMapPreview } from './full-screen.js';
import { coordinates, positionLine } from './position.js';
import { recordingBadge } from './recording.js';
import { watchForStall } from './stall.js';
import { openSingleView, orderedCameras, selectedCamera } from './view-state.js';
import { keepPlaying } from './whep.js';

// The key in `history.state` that marks the Map Preview's entry.
const HISTORY_MARKER = 'mapPreview';
const onOurEntry = () => history.state?.[HISTORY_MARKER] === true;

// Puts `text` on the clipboard and resolves true. Without the Clipboard API (a page served over
// plain HTTP is not a secure context) or when it refuses, it selects `node` instead, so the
// viewer can copy it with the keyboard, and resolves false.
async function copyText(text, node) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    getSelection().selectAllChildren(node);
    return false;
  }
}

// The previewed Device's Position, as the Single View header shows it (position.js), reading
// "No Position" while it has none, with a button that copies its coordinates. `show(label)`
// draws that Device's; it redraws on every `positions` event, and a stale age counts on.
function previewPosition(store) {
  const line = positionLine({ none: 'No Position' });
  const copy = element('button', 'btn btn-ghost map-preview-copy', 'Copy');
  copy.type = 'button';
  copy.setAttribute('aria-label', 'Copy coordinates');
  const copyStatus = element('span', 'map-preview-copied');
  copyStatus.setAttribute('role', 'status');
  const row = element('div', 'map-preview-position');
  row.append(line.element, copy, copyStatus);
  let copyStatusTimer = null;
  const sayBriefly = (text, ms) => {
    copyStatus.textContent = text;
    clearTimeout(copyStatusTimer);
    copyStatusTimer = setTimeout(() => { copyStatus.textContent = ''; }, ms);
  };

  let device = null;
  let receivedAt = Date.now();
  const draw = () => {
    const position = device ? store.get('positions')?.[device] ?? null : null;
    line.update(position, receivedAt);
    copy.hidden = !position;
  };
  store.on('positions', () => {
    receivedAt = Date.now();
    draw();
  });
  copy.addEventListener('click', async () => {
    const position = store.get('positions')?.[device];
    if (!position) return;
    if (await copyText(coordinates(position), line.coordinatesElement)) sayBriefly('Copied', 2000);
    else sayBriefly('Selected: press Ctrl+C or ⌘C to copy', 6000);
  });

  return {
    element: row,
    show(label) {
      if (label !== device) copyStatus.textContent = '';
      device = label;
      draw();
    },
  };
}

// The choices slot: a Device's Cameras in Camera order, each with its Feed Status, the playing
// one marked current. It shows only for a Device with two or more. Its list is named "Camera
// choices", never with "Cameras" in it, so it never matches the Management Bar's list by name.
// Returns fill(cameras, currentId), which updates the rows in place.
function cameraChoices(slot, onChoose) {
  const list = element('ul', 'map-preview-choice-list');
  list.setAttribute('aria-label', 'Camera choices');
  const rows = new Map(); // Camera id -> { item, button, name, status }

  const rowFor = (camera) => {
    const name = element('span', 'map-preview-choice-name');
    const status = element('span', 'feed-status');
    const button = element('button', 'map-preview-choice');
    button.type = 'button';
    button.append(name, status);
    button.addEventListener('click', () => onChoose(camera.id));
    const item = element('li');
    item.append(button);
    return { item, button, name, status };
  };

  return (cameras, currentId) => {
    const ids = new Set(cameras.map((camera) => camera.id));
    for (const [id, row] of rows) {
      if (ids.has(id)) continue;
      row.item.remove();
      rows.delete(id);
    }
    cameras.forEach((camera, index) => {
      if (!rows.has(camera.id)) rows.set(camera.id, rowFor(camera));
      const row = rows.get(camera.id);
      row.name.textContent = camera.name;
      setFeedStatus(row.status, camera.feed_status, camera.feed_status_reason);
      if (camera.id === currentId) row.button.setAttribute('aria-current', 'true');
      else row.button.removeAttribute('aria-current');
      if (list.children[index] !== row.item) list.insertBefore(row.item, list.children[index] ?? null);
    });
    slot.replaceChildren(...(cameras.length > 1 ? [list] : []));
  };
}

// root: Map View's section, holding the `.map` the panel lies over. Returns { open(device,
// opener), close() }; `opener` is any element, the one focus returns to when the panel closes
// with focus inside it or nowhere. It must still be on the page then, so keep it the same node.
export function mountMapPreview(root, store, { onInsets = () => {} } = {}) {
  const area = root.querySelector('.map');
  const panel = element('section', 'map-preview');
  panel.hidden = true;

  const title = element('h2', 'map-preview-title');
  const status = element('span', 'feed-status');
  const encryption = element('span', 'map-preview-slot');
  const recording = element('span', 'map-preview-slot');
  const closeButton = element('button', 'btn btn-ghost map-preview-close', '×');
  closeButton.type = 'button';
  closeButton.setAttribute('aria-label', 'Close Map Preview');
  closeButton.title = 'Close (Esc)';
  const header = element('header', 'map-preview-header');
  header.append(title, status, encryption, recording, closeButton);

  const video = element('video');
  Object.assign(video, { muted: true, autoplay: true, playsInline: true, disablePictureInPicture: true });
  const message = element('p', 'stage-message');
  const screen = element('div', 'stage map-preview-screen'); // the video and what lies over it
  screen.append(video, message);
  const empty = element('p', 'empty map-preview-empty', 'No Cameras');
  // Full Screen's corner button: it moves `screen` into Full Screen and back (ADR 0013), so the
  // Feed plays on with no second WHEP session. Esc is then Full Screen's (its dialog's).
  const fullScreenShows = mountFullScreenOnMapPreview(screen, store);

  const slots = {};
  for (const name of ['position', 'choices', 'actions']) {
    slots[name] = element('div', `map-preview-${name}`);
    slots[name].dataset.slot = name;
  }
  const position = previewPosition(store);
  slots.position.append(position.element);
  const toSingleView = element('button', 'btn btn-ghost map-preview-single', 'Open in Single View');
  toSingleView.type = 'button';
  slots.actions.append(toSingleView);
  panel.append(header, screen, empty, slots.position, slots.choices, slots.actions);
  root.append(panel); // beside the map, not in it, so its drags and wheels never reach MapLibre

  let device = null; // the Device label previewed, null while closed
  let opener = null;
  let feed = null; // keepPlaying handle of the Camera playing
  let playing = null; // that Camera's id
  const stall = watchForStall(screen, video, { session: () => feed?.session ?? null, store });

  const stop = () => {
    feed?.close();
    feed = null;
    playing = null;
    video.srcObject = null;
    message.textContent = '';
  };

  const play = (camera) => {
    stop(); // the old session closes before the new one opens
    playing = camera.id;
    feed = keepPlaying(video, camera.id, { onMessage: (text) => { message.textContent = text; } });
  };

  const deviceCameras = () => orderedCameras(store).filter((camera) => camera.device === device);
  // Switch to another of the Device's Cameras, closing the old session first (play's stop).
  const choose = (cameraId) => {
    const camera = deviceCameras().find((entry) => entry.id === cameraId);
    if (!camera || camera.id === playing) return;
    play(camera);
    render();
  };
  const fillChoices = cameraChoices(slots.choices, choose);

  // The Camera to show: the one playing while it lasts, else the one asked for on opening, else
  // the Device's first in Camera order.
  let wanted = null;
  const chosenCamera = () => {
    const cameras = deviceCameras();
    const byId = (id) => cameras.find((camera) => camera.id === id);
    return byId(playing) ?? byId(wanted) ?? cameras[0] ?? null;
  };

  let insets = { right: 0, bottom: 0 };
  const measure = () => {
    const next = { right: 0, bottom: 0 };
    if (device && !root.hidden) {
      const map = area.getBoundingClientRect();
      const box = panel.getBoundingClientRect();
      if (box.width < map.width / 2) next.right = Math.max(0, Math.round(map.right - box.left));
      else next.bottom = Math.min(Math.round(map.height), Math.max(0, Math.round(map.bottom - box.top)));
    }
    if (next.right === insets.right && next.bottom === insets.bottom) return;
    insets = next;
    onInsets(insets);
  };
  const sizes = new ResizeObserver(measure);
  sizes.observe(panel);
  sizes.observe(area);
  window.addEventListener('scroll', measure, { passive: true }); // the phone sheet stays put as the page scrolls

  const render = () => {
    if (!device) return;
    position.show(device);
    const camera = chosenCamera();
    const name = camera ? `${device} · ${camera.name}` : device;
    title.textContent = name;
    panel.setAttribute('aria-label', `Map Preview: ${name}`);
    status.hidden = !camera;
    if (camera) setFeedStatus(status, camera.feed_status, camera.feed_status_reason);
    encryption.replaceChildren(...(camera?.encrypted === false ? [unencryptedBadge()] : []));
    recording.replaceChildren(...[recordingBadge(camera?.recording)].filter(Boolean));
    stall.setLive(camera?.feed_status === 'live');
    screen.hidden = !camera;
    fullScreenShows(camera);
    toSingleView.hidden = !camera;
    empty.hidden = Boolean(camera);
    fillChoices(deviceCameras(), camera?.id);
    if (!camera) stop();
    else if (camera.id !== playing) play(camera);
  };

  // Set when the panel closes under another history user's entry (Full Screen, its Device
  // removed): its own entry is popped once that user's pop lands back on it.
  let popWhenUncovered = false;

  const open = (label, from = null, cameraId = null) => {
    if (label !== device) stop();
    popWhenUncovered = false;
    wanted = cameraId;
    device = label;
    opener = from;
    panel.hidden = false;
    if (!onOurEntry()) history.pushState({ [HISTORY_MARKER]: true }, '');
    render();
    measure();
    closeButton.focus(); // a keyboard or screen reader user lands in the panel they opened
  };

  // Whether closing should hand focus back to the opener: it is in the panel, or nowhere (a click
  // on something unfocusable in the panel drops it to the page). Focus the viewer moved elsewhere,
  // such as another marker or the Management Bar, stays where it is.
  const focusIsOurs = () => {
    const active = document.activeElement;
    return !active || active === document.body || panel.contains(active);
  };

  const close = ({ fromHistory = false } = {}) => {
    if (!device) return;
    const hadFocus = focusIsOurs();
    stop();
    device = null;
    fullScreenShows(null);
    panel.hidden = true;
    measure();
    if (onOurEntry()) history.back(); // its popstate finds the panel closed already
    else if (!fromHistory) popWhenUncovered = true;
    if (hadFocus && opener?.isConnected) opener.focus();
    opener = null;
  };

  closeButton.addEventListener('click', () => close());
  store.on('view', (view) => { if (view !== 'map') close(); });
  store.on('devices', (devices) => {
    if (device && !devices.some((entry) => entry.label === device)) close();
  });
  store.on('cameras', render);
  store.on('cameraOrder', render);

  // Bubbling, so the Management Bar's capture-phase handler closes an open bar first.
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !device || event.defaultPrevented || !escapeIsFree(event)) return;
    event.preventDefault();
    close();
  });
  window.addEventListener('popstate', () => {
    if (!onOurEntry()) close({ fromHistory: true });
    else if (popWhenUncovered && !device) {
      popWhenUncovered = false;
      history.back();
    }
  });
  window.addEventListener('pagehide', stop);

  // Open in Single View, and the return from it (see the header): `returning` outlives the
  // 'single' view and is spent by the next other one.
  let returning = false;
  toSingleView.addEventListener('click', () => {
    if (!playing) return;
    returning = true;
    openSingleView(store, playing);
  });
  store.on('view', (view) => {
    if (view === 'single') return;
    const reopen = returning && view === 'map';
    returning = false;
    const camera = reopen ? selectedCamera(store) : null;
    // After every listener of this change, so Map View shows and the close button takes focus.
    if (camera) queueMicrotask(() => { if (store.get('view') === 'map') open(camera.device, null, camera.id); });
  });

  return { open, close };
}
