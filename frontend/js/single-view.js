// Single View: plays the selected Camera's Feed while this browser's view is
// 'single'. Switching Cameras closes the previous WHEP session first; a
// Camera that disappears from the list stops, and so does leaving (Esc or the
// back button) for the View it was opened from, Multi View, which then plays
// its own tiles, or Map View; the back button names that View. A Feed that
// fails or drops is re-opened while it stays selected (keepPlaying). The
// header shows its Feed Status, and the stage a Stall overlay (stall.js).

import { setFeedStatus } from './feed-status.js';
import { recordingBadge, recordingControl } from './recording.js';
import { watchForStall } from './stall.js';
import { leaveSingleView, selectedCamera } from './view-state.js';
import { keepPlaying } from './whep.js';

function describe(camera) {
  const parts = [];
  if (camera.resolution) parts.push(`${camera.resolution.width}×${camera.resolution.height}`);
  return parts.join(' · ');
}

export function mountSingleView(root, store) {
  const video = root.querySelector('video');
  const title = root.querySelector('[data-title]');
  const meta = root.querySelector('[data-meta]');
  const message = root.querySelector('[data-message]');
  const status = root.querySelector('[data-feed-status]');
  const back = root.querySelector('[data-back]');
  back.addEventListener('click', () => leaveSingleView(store));
  const badgeSlot = root.querySelector('[data-recording]');
  const record = recordingControl();
  root.querySelector('.view-header').append(record.element);

  const showRecording = (camera) => {
    badgeSlot.replaceChildren(...[recordingBadge(camera?.recording)].filter(Boolean));
    record.update(camera);
  };

  let feed = null; // keepPlaying handle of the Camera on screen
  let playing = null; // Camera id currently opened
  const stall = watchForStall(root.querySelector('.stage'), video, { session: () => feed?.session ?? null, store });

  const stop = () => {
    feed?.close();
    feed = null;
    playing = null;
    video.srcObject = null;
  };

  const open = (camera) => {
    stop();
    playing = camera.id;
    title.textContent = `${camera.name} · ${camera.device}`;
    meta.textContent = describe(camera);
    feed = keepPlaying(video, camera.id, { onMessage: (text) => { message.textContent = text; } });
  };

  const update = () => {
    const showing = store.get('view') === 'single';
    root.hidden = !showing;
    if (!showing) {
      stop();
      return;
    }
    const camera = selectedCamera(store);
    status.hidden = !camera;
    if (camera) setFeedStatus(status, camera.feed_status, camera.feed_status_reason);
    stall.setLive(camera?.feed_status === 'live');
    showRecording(camera);
    if (!camera) {
      if (playing) {
        stop();
        title.textContent = 'Choose a Camera';
        meta.textContent = '';
        message.textContent = 'That Camera is gone. Pick another from the list.';
      }
      return;
    }
    meta.textContent = describe(camera);
    if (camera.id !== playing) open(camera);
  };

  store.on('view', update);
  store.on('singleFrom', (from) => { back.textContent = from === 'map' ? '← Map View' : '← Multi View'; });
  store.on('selected', update);
  store.on('cameras', update);
  window.addEventListener('pagehide', stop);
}
