// A Multi View tile: one Camera's Feed plus a bar that reads at a glance, in
// three rows so nothing is cut off on a narrow tile: the Device and Camera
// with the Feed Status and recording badges, then the Position and
// resolution, then the controls. The Device label leads; the Camera name
// follows it.
//
// The bar has named slots so other concerns can fill a tile without editing
// this module:
//
//   resolution  filled here from camera.resolution
//   position    Position and fix status (position.js's mountPositionOnTiles)
//   status      Feed Status badge      (feed-status.js's decorateTileStatus)
//   recording   recording badge        (left empty here; #10 fills it)
//   controls    interactive controls   (left empty here; #10's record buttons)
//
// Over the video, stall.js shows a Stall overlay when the Feed freezes in this
// browser while it is Live.
//
// A concern registers a decorator once with decorateTiles(fn). Every tile
// calls fn({ slots, camera, element, store }) when it is created and again
// whenever its Camera's server state changes (the `cameras` event carries
// feed_status, recording, and anything else a field provider adds). A
// decorator owns its slot's contents and should replace them on every call.
//
// Clicking anywhere on a tile opens it in Single View, except inside the
// controls slot: controls are buttons of their own, and their clicks never
// open Single View. Nor does the Full Screen button full-screen.js puts on
// the screen, which stops its own click. The title is the keyboard path to
// the same action.

import { watchForStall } from './stall.js';
import { keepPlaying } from './whep.js';

const decorators = [];

export function decorateTiles(decorator) {
  decorators.push(decorator);
}

export const SLOTS = ['resolution', 'position', 'status', 'encryption', 'recording', 'controls'];

function resolutionText(camera) {
  return camera.resolution ? `${camera.resolution.width}×${camera.resolution.height}` : '';
}

export function createTile(camera, { store, onOpen }) {
  const element = document.createElement('article');
  element.className = 'tile';
  element.dataset.camera = camera.id;
  element.setAttribute('aria-label', `${camera.name} · ${camera.device}`);

  const video = document.createElement('video');
  Object.assign(video, { muted: true, autoplay: true, playsInline: true, disablePictureInPicture: true });
  const message = document.createElement('p');
  message.className = 'tile-message';

  const open = document.createElement('button');
  open.type = 'button';
  open.className = 'tile-open';
  open.setAttribute('aria-label', `Open ${camera.name} · ${camera.device} in Single View`);
  const device = document.createElement('span');
  device.className = 'tile-device';
  device.textContent = camera.device;
  const name = document.createElement('span');
  name.className = 'tile-name';
  name.textContent = camera.name;
  open.append(device, name);

  const slots = {};
  for (const slot of SLOTS) {
    slots[slot] = document.createElement('span');
    slots[slot].className = `tile-slot tile-${slot}`;
    slots[slot].dataset.slot = slot;
  }
  const row = (className, ...children) => {
    const node = document.createElement('div');
    node.className = className;
    node.append(...children);
    return node;
  };
  const bar = row('tile-bar',
    row('tile-row tile-head', open, slots.status, slots.encryption, slots.recording),
    row('tile-row tile-info', slots.position, slots.resolution),
    row('tile-row tile-actions', slots.controls));

  // The screen holds the video and everything laid over it, so overlays cover
  // the picture alone however tall the bar grows.
  const screen = document.createElement('div');
  screen.className = 'tile-screen';
  screen.append(video, message);
  element.append(screen, bar);
  element.addEventListener('click', (event) => {
    if (slots.controls.contains(event.target)) return;
    onOpen(camera.id);
  });

  // The bar's real height, for the tile's least height (app.css): a 4.5rem
  // screen plus this, so no grid row can squeeze the video to nothing.
  const barSize = new ResizeObserver(() => element.style.setProperty('--bar-height', `${bar.offsetHeight}px`));
  barSize.observe(bar);

  let feed = null;
  const stall = watchForStall(screen, video, { session: () => feed?.session ?? null, store });

  const update = (next) => {
    camera = next;
    slots.resolution.textContent = resolutionText(camera);
    stall.setLive(camera.feed_status === 'live');
    for (const decorate of decorators) decorate({ slots, camera, element, store });
  };

  // Re-opened after a failure or drop for as long as the tile is on screen.
  const play = () => {
    feed = keepPlaying(video, camera.id, { onMessage: (text) => { message.textContent = text; } });
  };

  const close = () => {
    feed?.close();
    feed = null;
    stall.close();
    barSize.disconnect();
    video.srcObject = null;
    element.remove();
  };

  update(camera);
  play();
  return { element, slots, update, close, get session() { return feed?.session ?? null; } };
}
