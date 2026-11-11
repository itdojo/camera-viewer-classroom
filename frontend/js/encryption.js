// The "Unencrypted" warning (ADR 0014). A Camera whose source is plain
// rtsp:// reaches the Relay in the clear, so its tile carries an open-lock
// badge, and the Add via RTSP form says so while such a URL is typed. The
// server sends `encrypted` with every Camera; this module only renders it.

const OPEN_LOCK = '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" focusable="false">'
  + '<path d="M5 7V5a3 3 0 0 1 5.8-1.1" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>'
  + '<rect x="3" y="7" width="10" height="7" rx="1.5" fill="currentColor"/></svg>';

export const UNENCRYPTED_TITLE = 'This Camera is pulled over plain RTSP: the hop from the camera to the Relay is unencrypted';

export function unencryptedBadge() {
  const badge = document.createElement('span');
  badge.className = 'badge badge-unencrypted';
  badge.title = UNENCRYPTED_TITLE;
  badge.innerHTML = OPEN_LOCK;
  badge.append('Unencrypted');
  return badge;
}

// Multi View tile decorator (register with tile.js's decorateTiles).
export function decorateTileEncryption({ slots, camera }) {
  slots.encryption.replaceChildren(...(camera.encrypted === false ? [unencryptedBadge()] : []));
}

// Shows the form's [data-unencrypted] note while its URL field holds rtsp://.
export function warnOnPlainRtsp(form) {
  const url = form.elements.url;
  const note = form.querySelector('[data-unencrypted]');
  const check = () => { note.hidden = !/^rtsp:\/\//i.test(url.value.trim()); };
  url.addEventListener('input', check);
  form.addEventListener('reset', () => setTimeout(check)); // values clear after the event
  check();
}
