// The Feed Status badge: one pill per Feed, in the Camera list, the Single
// View header and each Multi View tile. The server sends each Camera's
// `feed_status` and, when something specific is wrong, `feed_status_reason`
// (e.g. "certificate changed"); this module only renders them. Colors live in
// app.css under .feed-status[data-feed-status=...].

const LABELS = {
  live: 'Live',
  degraded: 'Degraded',
  down: 'Down',
  idle: 'Idle',
  offline: 'Offline',
};

// Worst first, for a Device's marker on the map, which shows the worst of its Cameras'.
const WORST_FIRST = ['offline', 'down', 'degraded', 'idle', 'live'];

const known = (status) => (status in LABELS ? status : 'idle');

// "Down" for 'down'; an unknown status reads Idle, as on the badge.
export function feedStatusLabel(status) {
  return LABELS[known(status)];
}

// The worst of `statuses` (Offline, Down, Degraded, Idle, Live), or null when there are none.
export function worstFeedStatus(statuses) {
  const ranks = statuses.map((status) => WORST_FIRST.indexOf(known(status)));
  return ranks.length ? WORST_FIRST[Math.min(...ranks)] : null;
}

// Show `status` (and an optional reason) on an existing badge element.
export function setFeedStatus(badge, status, reason = null) {
  badge.dataset.feedStatus = known(status);
  badge.replaceChildren(feedStatusLabel(status));
  if (reason) {
    const why = document.createElement('span');
    why.className = 'feed-status-reason';
    why.textContent = ` · ${reason}`;
    badge.append(why);
  }
  badge.title = `Feed Status: ${feedStatusLabel(status)}${reason ? ` (${reason})` : ''}`;
}

// A new badge element showing `status`.
export function feedStatusBadge(status, reason = null) {
  const badge = document.createElement('span');
  badge.className = 'feed-status';
  setFeedStatus(badge, status, reason);
  return badge;
}

// Multi View tile decorator (register with tile.js's decorateTiles): fills the
// tile's `status` slot with the Camera's badge.
export function decorateTileStatus({ slots, camera }) {
  slots.status.replaceChildren(feedStatusBadge(camera.feed_status, camera.feed_status_reason));
}
