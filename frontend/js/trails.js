// Trail keeping, shared by every map in the UI: one Trail per Device, for whichever set of
// Devices a map shows, each fetched and extended on its own.
//
// When a Device joins the set, its Trail so far is asked of the instance
// (`GET /api/devices/<label>/trail`) without waiting for it: fixes from `positions` extend the
// line meanwhile, and the earlier fixes go in front of them when they land. The newest of those
// can be live fixes the line already has, so a live fix whose (gps_time, latitude, longitude)
// the answer's last fixes repeat is dropped. Only fresh GPS fixes extend it, never a stale or
// Fixed Position, and points older than the Trail's `seconds` drop off the end. A Device that
// leaves the set loses its Trail, and it is fetched afresh if it comes back. A Device whose
// Position is a Fixed Position (it has no fix, or its fix went stale) is out of the set, so no
// map draws a Trail behind one, even while the instance still holds its fixes. The Trail never
// travels over SSE.

import { request } from './api.js';

const fixKey = (fix) => `${fix.gps_time}|${fix.latitude}|${fix.longitude}`;

// `onFetched` runs whenever a Device's Trail so far lands, so the map can redraw it.
// `keep(devices, positions)` sets which Devices have a Trail: those of `devices` whose Position in
// the `positions` store value is a GPS fix. `extend(positions)` adds each one's fresh fix, and `points(device)` is a Trail as map.js draws it,
// `[{ latitude, longitude }]` oldest first, or [] for a Device with none.
export function createTrails(onFetched) {
  // label -> { points: [{ latitude, longitude, at, key }], seconds, last }, where `at` is when the
  // fix arrived at the instance by this browser's clock (ms), `key` is its (gps_time, latitude,
  // longitude), and `last` is the key of the fix that extended it last, since `positions`
  // re-sends every Device's Position whenever any Device moves.
  const trails = new Map();

  const start = (device) => {
    const mine = { points: [], seconds: null, last: null };
    trails.set(device, mine);
    request('GET', `/api/devices/${encodeURIComponent(device)}/trail`)
      .then(({ ok, data }) => {
        if (trails.get(device) !== mine || !ok) return;
        const now = Date.now();
        const earlier = data.fixes.map((fix) => ({ latitude: fix.latitude, longitude: fix.longitude, at: now - fix.age * 1000, key: fixKey(fix) }));
        // Only the answer's newest fixes can be live ones drawn meanwhile, at most one per live
        // point: GPS time repeats at every loop of a replayed track (ADR 0010), so an older fix
        // with the same key is another fix.
        const overlap = new Set(earlier.slice(Math.max(0, earlier.length - mine.points.length)).map((point) => point.key));
        mine.points = [...earlier, ...mine.points.filter((point) => !overlap.has(point.key))];
        mine.seconds = data.seconds;
        onFetched();
      })
      .catch(() => {}); // no Trail so far: the live fixes still draw it
  };

  const extendOne = (trail, position) => {
    if (position.source !== 'gps' || position.stale) return;
    const key = fixKey(position);
    if (key === trail.last) return;
    trail.last = key;
    trail.points.push({ latitude: position.latitude, longitude: position.longitude, at: Date.now() - (position.age ?? 0) * 1000, key });
  };

  return {
    keep(devices, positions) {
      const wanted = new Set(devices.filter((device) => positions?.[device]?.source === 'gps'));
      for (const device of trails.keys()) if (!wanted.has(device)) trails.delete(device);
      for (const device of wanted) if (!trails.has(device)) start(device);
    },
    extend(positions) {
      for (const [device, trail] of trails) {
        const position = positions?.[device];
        if (position) extendOne(trail, position);
      }
    },
    points(device) {
      const trail = trails.get(device);
      if (!trail) return [];
      if (trail.seconds !== null) {
        const since = Date.now() - trail.seconds * 1000;
        trail.points = trail.points.filter((point) => point.at >= since);
      }
      return trail.points;
    },
  };
}
