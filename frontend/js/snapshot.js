// Snapshot: the frame on screen, saved as a PNG Capture on the instance.
//
// The frame is drawn onto a canvas at the video's intrinsic size (the Feed's
// received resolution), never at the size it is shown, and uploaded as a
// PNG. The server checks it, names it like every Capture and answers with
// the file name, which the control shows in its status line.
//
// Single View gets a Snapshot button in its header; each Multi View tile gets
// one in its `controls` slot, next to Record (clicks there never open Single
// View).

import { toLoginWall } from './api.js';
import { decorateTiles } from './tile.js';

const STATUS_CLEARS_AFTER_MS = 6000;

// The video's current frame as a PNG at its full resolution.
export function frameAsPng(video) {
  const canvas = document.createElement('canvas');
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('the browser could not encode the frame'))),
      'image/png');
  });
}

// Upload the frame on screen as a Snapshot of cameraId. Resolves to the
// server's answer ({capture, camera, width, height, size}); throws with a
// message a student can read.
export async function takeSnapshot(video, cameraId) {
  if (!video.videoWidth || !video.videoHeight || video.readyState < 2) {
    throw new Error('the Feed is not playing yet');
  }
  const png = await frameAsPng(video);
  const response = await fetch(`/api/snapshots?camera=${encodeURIComponent(cameraId)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'image/png' },
    body: png,
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    // empty or non-JSON body
  }
  if (response.status === 401) {
    toLoginWall();
    return new Promise(() => {}); // the page is leaving
  }
  if (!response.ok) throw new Error(data?.message ?? `the instance answered ${response.status}`);
  return data;
}

// A Snapshot button plus a polite status line. target() returns the
// {video, cameraId} to capture at click time, or null when there is none.
// compact: say only "Saved" (the file name goes in the tooltip), for tiles.
export function snapshotControl(target, { compact = false } = {}) {
  const element = document.createElement('span');
  element.className = 'snapshot-control';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn btn-snapshot';
  button.textContent = 'Snapshot';
  const status = document.createElement('span');
  status.className = 'snapshot-status';
  status.setAttribute('role', 'status');
  element.append(button, status);

  let busy = false;
  let clearTimer = null;
  const say = (text, title = '', clears = true) => {
    clearTimeout(clearTimer);
    status.textContent = text;
    status.title = title;
    if (clears) clearTimer = setTimeout(() => say('', '', false), STATUS_CLEARS_AFTER_MS);
  };

  button.addEventListener('click', async () => {
    const current = target();
    if (!current || busy) return;
    busy = true;
    button.disabled = true;
    say('Saving…', '', false);
    try {
      const saved = await takeSnapshot(current.video, current.cameraId);
      say(compact ? 'Saved' : `Saved ${saved.capture}`, saved.capture);
    } catch (error) {
      say(compact ? 'Failed' : `Snapshot failed: ${error.message}`, error.message);
    } finally {
      busy = false;
      button.disabled = false;
    }
  });

  return {
    element,
    update(cameraId) {
      element.hidden = !cameraId;
    },
  };
}

// Single View: a Snapshot control in the header, for the selected Camera.
export function mountSnapshotOnSingleView(root, store) {
  const video = root.querySelector('video');
  const control = snapshotControl(() => {
    const cameraId = store.get('selected');
    return cameraId ? { video, cameraId } : null;
  });
  root.querySelector('.view-header').append(control.element);
  const update = () => control.update(store.get('selected'));
  store.on('selected', update);
  update();
}

// Multi View: a Snapshot control in each tile's `controls` slot. Recording
// fills that slot too (with replaceChildren when the tile is created), so
// this control re-appends itself whenever it finds itself missing, whatever
// order the two decorators were registered in.
export function mountSnapshotOnTiles() {
  const controls = new WeakMap(); // tile element -> its control
  decorateTiles(({ slots, camera, element }) => {
    if (!controls.has(element)) {
      const video = element.querySelector('video');
      controls.set(element, snapshotControl(() => ({ video, cameraId: camera.id }), { compact: true }));
    }
    const control = controls.get(element);
    control.update(camera.id);
    const place = () => {
      if (!slots.controls.contains(control.element)) slots.controls.append(control.element);
    };
    place();
    queueMicrotask(place); // after any decorator that replaces the slot's contents in this pass
  });
}
