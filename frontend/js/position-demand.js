// Position Demand renewer: tell the instance every 10 seconds that a set of
// Devices' Positions are wanted at full rate (ADR 0012). While this browser
// has a Camera in Single View, the set is that Camera's Device; while it is in
// Map View, every Device with a GPS Sidecar. The instance lets each Demand
// lapse 25 seconds after its last renewal, so leaving, closing the tab or a
// crash all end it with nothing to send. Driven only by the store: `view`,
// `selected`, the `cameras` list that says which Device a Camera belongs to,
// and the `devices` list that says which Devices have a GPS Sidecar.

import { request } from './api.js';
import { selectedCamera } from './view-state.js';

export const RENEW_MS = 10_000;

// The labels of the Devices whose Position this browser wants at full rate.
function wantedDevices(store) {
  if (store.get('view') === 'map') {
    return (store.get('devices') ?? []).filter((device) => device.gps_sidecar).map((device) => device.label);
  }
  const camera = store.get('view') === 'single' ? selectedCamera(store) : undefined;
  return camera ? [camera.device] : [];
}

export function mountPositionDemand(store) {
  const timers = new Map(); // label of each Device being renewed -> its interval

  const renew = (device) => {
    request('PUT', `/api/devices/${encodeURIComponent(device)}/position-demand`).catch(() => {
      // offline or refused: the next renewal tries again, and the Demand lingers meanwhile
    });
  };

  // A Device joining the set is renewed at once, so the fast rate starts within a second or
  // two, then every RENEW_MS until it leaves. One staying in the set keeps its own schedule, so
  // switching between Cameras of one Device sends no extra renewal.
  const update = () => {
    const wanted = new Set(wantedDevices(store));
    for (const [device, timer] of timers) {
      if (wanted.has(device)) continue;
      clearInterval(timer);
      timers.delete(device);
    }
    for (const device of wanted) {
      if (timers.has(device)) continue;
      renew(device);
      timers.set(device, setInterval(() => renew(device), RENEW_MS));
    }
  };

  for (const key of ['view', 'selected', 'cameras', 'devices']) store.on(key, update);
}
