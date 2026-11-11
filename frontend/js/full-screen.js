// Full Screen: one Feed filling the device's whole screen, over whichever view
// it came from (ADR 0013). Every browser gets the same thing: a modal <dialog>
// over the window, into which the tile's or the stage's screen (its video,
// message and Stall overlay) moves, and back out on exit. Both moves are
// synchronous, so the video keeps playing and no WHEP session is reopened.
// Where the browser has element fullscreen (Android, desktop, iPad), the
// dialog's frame also goes fullscreen, which hides the browser's own bars; a
// <dialog> itself may not (Fullscreen spec), hence the frame. On iPhone there
// is no element fullscreen, and the dialog alone is as full as Safari allows,
// address bar and all. The iPhone's native player is never used, because it
// would drop every overlay.
//
// It is entered from the corner button on every tile, on the Single View stage
// and on the Map Preview, or in Single View or a playing Map Preview by turning
// a phone to landscape. Turning back exits only a Full Screen that the turn
// opened. Every exit (the exit button, Esc, Back, the browser leaving
// fullscreen, the turn back) ends the dialog and the fullscreen together. Back
// works because entering pushes one history entry, which any other exit pops
// again.
//
// The header (Device · Camera, Feed Status, recording badge, exit) hides after
// HEADER_HIDES_AFTER_MS without a touch and comes back on a tap, but stays
// while the Feed is not Live. A drop keeps Full Screen up while keepPlaying
// reconnects. Only the Camera going away, or its view taking the screen back
// (a tile closing), ends it. It holds a screen wake lock where the browser
// grants one (HTTPS only). Nothing here is saved, so a reload is never in Full
// Screen.

import { setFeedStatus } from './feed-status.js';
import { SHORT_LANDSCAPE } from './multi-view.js';
import { recordingBadge } from './recording.js';
import { decorateTiles } from './tile.js';
import { selectedCamera } from './view-state.js';

const HEADER_HIDES_AFTER_MS = 3000;

const SVG = 'http://www.w3.org/2000/svg';
const EXPAND = ['M2 6V2h4', 'M10 2h4v4', 'M14 10v4h-4', 'M6 14H2v-4'];
const COLLAPSE = ['M6 2v4H2', 'M10 2v4h4', 'M14 10h-4v4', 'M2 10h4v4'];

function icon(paths) {
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.classList.add('icon');
  for (const d of paths) {
    const path = document.createElementNS(SVG, 'path');
    path.setAttribute('d', d);
    svg.append(path);
  }
  return svg;
}

function openButton() {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'full-screen-open';
  button.title = 'Full Screen';
  button.append(icon(EXPAND));
  return button;
}

const nameOf = (camera) => `Full Screen ${camera.name} · ${camera.device}`;

// A phone on its side by the screen's own orientation, which can be ahead of
// the media query when screen.orientation's `change` event runs.
const phoneOnItsSide = () =>
  screen.orientation.type.startsWith('landscape') && Math.min(screen.width, screen.height) <= 540;

function createFullScreen(store) {
  const dialog = document.createElement('dialog');
  dialog.className = 'full-screen';
  dialog.setAttribute('aria-label', 'Full Screen');
  const frame = document.createElement('div');
  frame.className = 'full-screen-frame';
  const header = document.createElement('header');
  header.className = 'full-screen-header';
  const title = document.createElement('h2');
  title.className = 'full-screen-title';
  const status = document.createElement('span');
  status.className = 'feed-status';
  const badge = document.createElement('span');
  const exit = document.createElement('button');
  exit.type = 'button';
  exit.className = 'full-screen-exit';
  exit.setAttribute('aria-label', 'Exit Full Screen');
  exit.title = 'Exit Full Screen (Esc)';
  exit.autofocus = true;
  exit.append(icon(COLLAPSE));
  header.append(title, status, badge, exit);
  frame.append(header);
  dialog.append(frame);
  document.body.append(dialog);

  // { screen, placeholder, cameraId, opener, byTurn } while open, else null.
  let shown = null;
  let hideTimer = null;
  let wasPinned = false;
  let lock = null;
  let poppingOurEntry = false;
  let fullscreenSettled = Promise.resolve(); // the last request, until it and any exit it needed end

  const camera = () => (store.get('cameras') ?? []).find((c) => c.id === shown?.cameraId);
  const pinned = () => camera()?.feed_status !== 'live';

  const conceal = () => {
    if (!shown || pinned()) return;
    clearTimeout(hideTimer);
    dialog.dataset.chrome = 'hidden';
  };
  const reveal = () => {
    clearTimeout(hideTimer);
    delete dialog.dataset.chrome;
    if (!pinned()) hideTimer = setTimeout(conceal, HEADER_HIDES_AFTER_MS);
  };

  const renderHeader = () => {
    const cam = camera();
    title.textContent = `${cam.device} · ${cam.name}`;
    setFeedStatus(status, cam.feed_status, cam.feed_status_reason);
    badge.replaceChildren(...[recordingBadge(cam.recording)].filter(Boolean));
    const now = pinned();
    if (now || wasPinned) reveal(); // pinned on a drop; back on its timer once Live again
    wasPinned = now;
  };

  const holdWakeLock = () => {
    if (!shown || document.hidden || !navigator.wakeLock || (lock && !lock.released)) return;
    navigator.wakeLock.request('screen').then((sentinel) => {
      if (shown) lock = sentinel;
      else sentinel.release().catch(() => {});
    }).catch(() => {}); // plain HTTP, or refused: the screen may sleep
  };
  const releaseWakeLock = () => {
    lock?.release().catch(() => {});
    lock = null;
  };

  // The browser's own fullscreen, where it has one. A refusal (no tap, or an
  // iPhone) leaves the dialog alone over the window, which is still Full Screen.
  const goFull = () => {
    if (!shown || document.fullscreenElement || !document.fullscreenEnabled) return;
    fullscreenSettled = frame.requestFullscreen({ navigationUI: 'hide' })
      .then(() => { if (!shown) return document.exitFullscreen(); })
      .catch(() => {});
  };

  const open = (screen, cameraId, { opener = null, byTurn = false } = {}) => {
    if (shown || !(store.get('cameras') ?? []).some((c) => c.id === cameraId)) return;
    const placeholder = document.createComment('full-screen');
    screen.before(placeholder);
    shown = { screen, placeholder, cameraId, opener, byTurn };
    frame.prepend(screen); // synchronous with its removal, so the video keeps playing
    wasPinned = pinned();
    renderHeader();
    dialog.showModal();
    reveal();
    history.pushState({ fullScreen: true }, '');
    goFull();
    holdWakeLock();
  };

  const close = ({ fromHistory = false } = {}) => {
    if (!shown) return;
    const { screen, placeholder, opener } = shown;
    shown = null;
    clearTimeout(hideTimer);
    placeholder.replaceWith(screen); // synchronous again: the video plays on where it came from
    if (dialog.open) dialog.close();
    const leaving = document.fullscreenElement ? document.exitFullscreen().catch(() => {}) : null;
    releaseWakeLock();
    if (!fromHistory) {
      poppingOurEntry = true;
      history.back();
    }
    // Focus can't land outside the fullscreen element, and the browser's
    // fullscreen ends a moment after the dialog, or begins and then ends if
    // its request was still in flight. So the opener gets focus back after both.
    Promise.all([fullscreenSettled, leaving]).then(() => {
      if (!shown && opener?.isConnected) opener.focus({ preventScroll: true });
    });
  };

  exit.addEventListener('click', () => close());
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault(); // Esc: close() puts the screen back before the dialog goes
    close();
  });
  dialog.addEventListener('close', () => close()); // closed some other way
  frame.addEventListener('click', (event) => {
    if (header.contains(event.target)) return;
    if (dialog.dataset.chrome === 'hidden') reveal();
    else conceal();
  });
  frame.addEventListener('pointermove', (event) => { if (event.pointerType === 'mouse') reveal(); });
  frame.addEventListener('focusin', reveal);
  document.addEventListener('fullscreenchange', () => {
    if (!document.fullscreenElement) close(); // Esc or Back in the browser's fullscreen
  });
  window.addEventListener('popstate', () => {
    if (poppingOurEntry) {
      poppingOurEntry = false;
      return;
    }
    close({ fromHistory: true });
  });
  document.addEventListener('visibilitychange', holdWakeLock); // the browser drops it while hidden

  // After every listener of the same change has run, so a tile Multi View
  // closed in the meantime has already taken its screen's place away.
  const follow = () => queueMicrotask(() => {
    if (!shown) return;
    if (!camera() || !shown.placeholder.isConnected) close();
    else renderHeader();
  });
  for (const key of ['cameras', 'onScreen', 'view']) store.on(key, follow);

  return {
    open,
    goFull,
    isOpen: () => shown !== null,
    // Turning back to portrait ends only a Full Screen that the turn opened.
    closeIfTurned: () => { if (shown?.byTurn) close(); },
  };
}

let theOne = null;
const fullScreenFor = (store) => (theOne ??= createFullScreen(store));

// Every Multi View tile's corner button. Register before Multi View creates
// its first tile.
export function mountFullScreenOnTiles(store) {
  const fullScreen = fullScreenFor(store);
  const buttons = new WeakMap(); // tile element -> its button
  decorateTiles(({ camera, element }) => {
    let button = buttons.get(element);
    if (!button) {
      const screen = element.querySelector('.tile-screen');
      button = openButton();
      button.addEventListener('click', (event) => {
        event.stopPropagation(); // a tile's click opens Single View
        fullScreen.open(screen, element.dataset.camera, { opener: button });
      });
      screen.append(button);
      buttons.set(element, button);
    }
    button.setAttribute('aria-label', nameOf(camera));
  });
}

// The Single View stage's corner button, and turning a phone to landscape.
export function mountFullScreenOnSingleView(root, store) {
  const fullScreen = fullScreenFor(store);
  const stage = root.querySelector('.stage');
  const button = openButton();
  stage.append(button);

  const enter = (options) => {
    const camera = selectedCamera(store);
    if (camera && store.get('view') === 'single') fullScreen.open(stage, camera.id, options);
  };
  button.addEventListener('click', () => enter({ opener: button }));

  const label = () => {
    const camera = selectedCamera(store);
    button.hidden = !camera;
    if (camera) button.setAttribute('aria-label', nameOf(camera));
  };
  store.on('cameras', label);
  store.on('selected', label);
  enterOnTurn(fullScreen, enter);
}

// The Map Preview's corner button, and turning a phone to landscape while the
// panel plays. Returns `fullScreenShows(camera)`, which the panel calls with the
// Camera it plays, or null while it plays none (closed, or a Device with no
// Cameras).
export function mountFullScreenOnMapPreview(screenNode, store) {
  const fullScreen = fullScreenFor(store);
  const button = openButton();
  screenNode.append(button);
  let cameraId = null;

  const enter = (options) => {
    if (cameraId && store.get('view') === 'map') fullScreen.open(screenNode, cameraId, options);
  };
  button.addEventListener('click', () => enter({ opener: button }));
  enterOnTurn(fullScreen, enter);

  return function fullScreenShows(camera) {
    cameraId = camera?.id ?? null;
    if (camera) button.setAttribute('aria-label', nameOf(camera));
  };
}

// Turning a phone to landscape enters Full Screen through `enter`, which opens
// it only when its view is showing; turning back exits only a Full Screen that
// a turn opened.
function enterOnTurn(fullScreen, enter) {
  const turn = (onItsSide) => {
    if (!onItsSide) {
      fullScreen.closeIfTurned();
      return;
    }
    if (!fullScreen.isOpen()) enter({ byTurn: true });
    fullScreen.goFull(); // allowed from screen.orientation's `change`, refused from the media query
  };
  const sideways = window.matchMedia(SHORT_LANDSCAPE);
  sideways.addEventListener('change', () => turn(sideways.matches));
  screen.orientation?.addEventListener('change', () => turn(phoneOnItsSide()));
}
