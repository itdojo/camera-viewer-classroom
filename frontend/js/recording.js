// Recording: the badge and the Record / Stop control, shared by every view.
// A Recording belongs to the instance, so both read only the server's
// `cameras` state (each Camera carries its unfinished `recording`, or null);
// nothing here is per browser. The Camera list, Single View and the Multi
// View tiles all render the same badge and control from these helpers.
//
// A timed Recording carries `remaining` (seconds, as of when the server sent
// it); its badge counts down locally from there, so the countdown needs no
// event per second and doesn't depend on this browser's clock.

import { request } from './api.js';
import { decorateTiles } from './tile.js';

const BADGE_TEXT = {
  waiting: 'Waiting for Feed',
  active: 'Recording',
  stopping: 'Finishing',
};

// -- timed Recordings ----------------------------------------------------------

// "12 s", "1 min", "5 min", "1 min 30 s": a preset as a student reads it.
export function presetLabel(seconds) {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (!minutes) return `${rest} s`;
  return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
}

let presets = null; // one request per page: the instance's preset durations, in seconds
function loadPresets() {
  presets ??= request('GET', '/api/recordings/presets').then(({ ok, data }) => (ok ? data.presets : []));
  return presets;
}

// When each timed Recording ends, on this page's monotonic clock, fixed the
// first time the page hears of it; every badge for it counts down to that.
const deadlines = new Map(); // recording id -> performance.now() deadline
function deadlineOf(recording) {
  if (!deadlines.has(recording.id)) deadlines.set(recording.id, performance.now() + recording.remaining * 1000);
  return deadlines.get(recording.id);
}

function leftText(deadline) {
  const seconds = Math.max(0, Math.ceil((deadline - performance.now()) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')} left`;
}

let ticking = null; // one interval updates every countdown on the page
function tickCountdowns() {
  const countdowns = document.querySelectorAll('.badge-remaining');
  countdowns.forEach((el) => { el.textContent = leftText(Number(el.dataset.deadline)); });
  if (!countdowns.length) {
    clearInterval(ticking);
    ticking = null;
  }
}

function countdown(recording) {
  const el = document.createElement('span');
  el.className = 'badge-remaining';
  el.dataset.deadline = String(deadlineOf(recording));
  el.textContent = leftText(Number(el.dataset.deadline));
  ticking ??= setInterval(tickCountdowns, 250);
  return el;
}

// -- badge and control -----------------------------------------------------------

// A badge for a Camera's Recording, or null when it has none. A timed one
// also counts down the time it has left.
export function recordingBadge(recording) {
  if (!recording) return null;
  const badge = document.createElement('span');
  badge.className = 'badge badge-recording';
  badge.dataset.state = recording.state;
  const text = document.createElement('span');
  text.textContent = BADGE_TEXT[recording.state] ?? 'Recording';
  badge.append(text);
  if (recording.remaining != null && recording.state !== 'stopping') badge.append(countdown(recording));
  return badge;
}

// duration: one of the instance's presets in seconds, or null until stopped.
export async function startRecording(cameraId, duration = null) {
  return request('POST', '/api/recordings', duration ? { camera: cameraId, duration } : { camera: cameraId });
}

export async function stopRecording(recordingId) {
  return request('POST', `/api/recordings/${encodeURIComponent(recordingId)}/stop`);
}

// A Duration picker and a Record / Stop recording button for one Camera.
// Call update(camera) whenever that Camera's state changes; the control
// follows its Recording. The picker shows only while there is none. A
// refused start (low disk, say) shows the server's reason in a status line;
// compact (tiles) says only "Refused", with the reason in the tooltip.
export function recordingControl({ compact = false } = {}) {
  const element = document.createElement('span');
  element.className = 'record-control';
  const duration = document.createElement('select');
  duration.className = 'record-duration';
  duration.setAttribute('aria-label', 'Duration');
  duration.append(new Option('Until stopped', ''));
  loadPresets().then((seconds) => {
    duration.append(...seconds.map((s) => new Option(presetLabel(s), String(s))));
  });
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn btn-record';
  const status = document.createElement('span');
  status.className = 'record-status';
  status.setAttribute('role', 'status');
  element.append(duration, button, status);
  const say = (message = '') => {
    status.textContent = message && compact ? 'Refused' : message;
    status.title = message;
  };
  let camera = null;
  let busy = false;

  const render = () => {
    const recording = camera?.recording;
    element.hidden = !camera;
    duration.hidden = Boolean(recording);
    duration.disabled = busy;
    button.textContent = recording ? 'Stop recording' : 'Record';
    button.disabled = busy || recording?.state === 'stopping';
  };

  button.addEventListener('click', async () => {
    if (!camera || busy) return;
    busy = true;
    render();
    const recording = camera.recording;
    try {
      if (recording) {
        await stopRecording(recording.id);
      } else {
        const { ok, data } = await startRecording(camera.id, Number(duration.value) || null);
        say(ok ? '' : (data?.message ?? 'The Recording did not start.'));
      }
    } finally {
      busy = false;
      render();
    }
  });

  render();
  return {
    element,
    update(next) {
      if (next?.id !== camera?.id) say(); // a refusal belongs to the Camera it was for
      camera = next ?? null;
      render();
    },
  };
}

// Multi View tiles: the badge in the tile's `recording` slot and the control
// in its `controls` slot (clicks there never open Single View). Each tile
// keeps one control, so a click in flight survives the re-render it causes.
export function mountRecordingOnTiles() {
  const controls = new WeakMap(); // tile element -> its control
  decorateTiles(({ slots, camera, element }) => {
    slots.recording.replaceChildren(...[recordingBadge(camera.recording)].filter(Boolean));
    if (!controls.has(element)) {
      const control = recordingControl({ compact: true });
      controls.set(element, control);
      slots.controls.replaceChildren(control.element);
    }
    controls.get(element).update(camera);
  });
}
