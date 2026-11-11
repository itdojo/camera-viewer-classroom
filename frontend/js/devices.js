// Devices: the list with a Remove button and a Position field each, the add
// form, and the manual Camera form (Add via RTSP), both with trust on first
// use.
//
// A trust-on-first-use form's first submit sends no fingerprint; the instance
// answers 409 `fingerprint_unconfirmed` with the fingerprint the Device
// presents, which is shown for the student to compare with gen-cert.sh's
// output. The second submit sends exactly that fingerprint back as the
// confirmed one. Editing the address (or URL) starts over. Labels and names
// are checked by the instance; its message is shown as it is.
//
// Each entry's Position field says where the Device is and from what: "GPS"
// for a fix, "Fixed" for a Fixed Position, "None" for neither, read from the
// `positions` store key, whose precedence already picks between them. Opened,
// it holds the Fixed Position form: a latitude and longitude typed by hand for
// a Device with no GPS receiver. The instance alone checks it, so the field
// sends what was typed (a number when it reads as one) and shows the
// instance's refusal. The form's values come from the `fixed_positions` key.
// A Device with a fresh 3D GPS fix gets no form: its field cannot be opened.
//
// The list is in this browser's order (`deviceOrder`, view-state.js).
// Alt+↑/↓ on a Device's grip, or dragging it, moves the Device (reorder.js).
// Entries are kept by label and updated in place, since `devices` arrives on
// every clock, certificate and `gps_sidecar` change: a redrawn entry would
// lose typing, focus and a drag in progress.

import { request } from './api.js';
import { coordinates } from './position.js';
import { moved, placeInOrder, reorderable } from './reorder.js';
import { inOrder } from './view-state.js';

const NUMBER = /^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/i;

// What a coordinate field sends: a number when the text reads as one, else the
// text itself (or null when empty), for the instance to refuse with its reason.
function coordinate(text) {
  const trimmed = text.trim();
  if (trimmed === '') return null;
  return NUMBER.test(trimmed) ? Number(trimmed) : trimmed;
}

// The source word for each kind of Position, and for none.
const SOURCE = { gps: 'GPS', fixed: 'Fixed', none: 'None' };

// One Device's Position field: a disclosure whose summary shows where the
// Device is and from what, and a form to set, change or clear its Fixed
// Position.
function positionField(label) {
  const element = document.createElement('details');
  element.className = 'fixed-position';
  const summary = document.createElement('summary');
  const heading = document.createElement('span');
  heading.textContent = 'Position';
  const source = document.createElement('span');
  source.className = 'fixed-position-source';
  const current = document.createElement('span');
  current.className = 'fixed-position-current';
  summary.append(heading, source, current);
  const form = document.createElement('form');
  form.className = 'form fixed-position-form';
  form.setAttribute('aria-label', `Fixed Position of ${label}`);
  form.autocomplete = 'off';
  const input = (name, placeholder) => {
    const field = document.createElement('label');
    const control = document.createElement('input');
    control.name = name;
    control.inputMode = 'decimal';
    control.placeholder = placeholder;
    field.append(name[0].toUpperCase() + name.slice(1), control);
    return { field, control };
  };
  const latitude = input('latitude', '51.47793');
  const longitude = input('longitude', '-0.00148');
  const error = document.createElement('p');
  error.className = 'form-error';
  error.setAttribute('role', 'alert');
  const save = document.createElement('button');
  save.type = 'submit';
  save.className = 'btn btn-quiet';
  save.textContent = 'Save';
  const clear = document.createElement('button');
  clear.type = 'button';
  clear.className = 'btn btn-quiet';
  clear.textContent = 'Clear';
  const actions = document.createElement('div');
  actions.className = 'fixed-position-actions';
  actions.append(save, clear);
  form.append(latitude.field, longitude.field, error, actions);
  element.append(summary, form);

  // Where the Device is, from its Position (or null): the summary. While a
  // fresh 3D fix holds, the GPS is the Position and a Fixed Position would
  // never show, so the field stays shut and offers no form.
  let locked = false;
  const showPosition = (position) => {
    const kind = position ? position.source : 'none';
    source.textContent = SOURCE[kind] ?? kind;
    source.dataset.source = kind;
    current.textContent = position ? coordinates(position) : '';
    element.toggleAttribute('data-stale', position?.stale === true);
    locked = kind === 'gps' && !position.stale && position.fix === '3d';
    element.toggleAttribute('data-locked', locked);
    if (locked) element.open = false;
    if (locked) summary.setAttribute('aria-disabled', 'true');
    else summary.removeAttribute('aria-disabled');
    summary.title = locked ? 'Set by a 3D GPS fix. A Fixed Position can be entered once the fix is lost.'
      : position?.stale ? 'Last known GPS fix' : '';
  };
  summary.addEventListener('click', (event) => {
    if (locked) event.preventDefault();
  });

  let edited = false; // typed into since the last value shown: an update must not overwrite it
  // The Fixed Position (or null): the form.
  const show = (place) => {
    clear.disabled = !place;
    if (edited) return;
    latitude.control.value = place ? String(place.latitude) : '';
    longitude.control.value = place ? String(place.longitude) : '';
  };
  form.addEventListener('input', () => { edited = true; });

  const send = async (method, body) => {
    error.textContent = '';
    save.disabled = true;
    try {
      const response = await request(method, `/api/devices/${encodeURIComponent(label)}/fixed-position`, body);
      if (response.ok) {
        edited = false;
        show(method === 'DELETE' ? null : response.data);
      } else {
        error.textContent = response.data?.message ?? `That failed (HTTP ${response.status}).`;
      }
    } catch (failure) {
      error.textContent = `That failed: ${failure.message}`;
    } finally {
      save.disabled = false;
    }
  };
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    send('PUT', { latitude: coordinate(latitude.control.value), longitude: coordinate(longitude.control.value) });
  });
  clear.addEventListener('click', () => send('DELETE'));

  return { element, show, showPosition };
}

// One Device's entry, kept across `devices` events: `update(device)` redraws
// what may change, and `position` is its Position field.
function deviceItem(device, onRemove) {
  const item = document.createElement('li');
  item.dataset.device = device.label;
  const head = document.createElement('div');
  head.className = 'device-head';
  const label = document.createElement('span');
  label.className = 'device-label';
  label.textContent = device.label;
  label.id = `device-item-${encodeURIComponent(device.label)}`;
  // Named "Move" alone, with the label as its description, as the Camera list's grips are.
  const grip = document.createElement('button');
  grip.type = 'button';
  grip.className = 'item-grip';
  grip.setAttribute('aria-label', 'Move');
  grip.setAttribute('aria-describedby', label.id);
  grip.setAttribute('aria-keyshortcuts', 'Alt+ArrowUp Alt+ArrowDown');
  grip.title = 'Drag, or press Alt+↑/↓, to move this Device';
  const dots = document.createElement('span');
  dots.setAttribute('aria-hidden', 'true');
  dots.textContent = '⠿';
  grip.append(dots);
  let current = device;
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'btn btn-quiet';
  remove.textContent = 'Remove';
  remove.setAttribute('aria-label', `Remove ${device.label}`);
  remove.addEventListener('click', () => onRemove(current, remove));
  head.append(grip, label, remove);
  const meta = document.createElement('span');
  meta.className = 'meta';
  const warning = document.createElement('span');
  warning.className = 'device-warning';
  const position = positionField(device.label);
  item.append(head, meta, warning, position.element);

  item.position = position;
  item.update = (next) => {
    current = next;
    const cameras = next.cameras.length === 1 ? '1 Camera' : `${next.cameras.length} Cameras`;
    meta.textContent = `${next.address} · ${cameras}${next.api ? '' : ' · manual'}`;
    warning.textContent = clockNote(next);
    warning.hidden = !next.clock_wrong;
  };
  item.update(device);
  return item;
}

// A Device whose clock is more than an hour off the instance's refuses the
// Relay's RTSPS pulls, and its Feeds show Down "Device clock wrong". This says
// by how much, and what fixes it.
function clockNote(device) {
  if (!device.clock_wrong) return '';
  const skew = device.clock_skew_seconds;
  const minutes = Math.round(Math.abs(skew) / 60);
  const span = minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : `${minutes} min`;
  return `${device.label}'s clock is ${span} ${skew < 0 ? 'behind' : 'ahead of'} this instance's, `
    + 'so its Feeds cannot start. Enable NTP on the Device, or set its clock.';
}

function trustOnFirstUse(form, { path, addressField, submitLabel, onAdded }) {
  const panel = form.querySelector('[data-fingerprint]');
  const value = form.querySelector('[data-fingerprint-value]');
  const error = form.querySelector('[data-error]');
  const note = form.querySelector('[data-note]');
  const submit = form.querySelector('[data-submit]');
  let presented = null;

  const showFingerprint = (fingerprint) => {
    presented = fingerprint;
    value.textContent = fingerprint ? fingerprint.match(/.{1,2}/g).join(':') : '';
    panel.hidden = !fingerprint;
    submit.textContent = fingerprint ? `Confirm fingerprint and ${submitLabel}` : submit.dataset.initial;
  };
  submit.dataset.initial = submit.textContent;

  form[addressField].addEventListener('input', () => showFingerprint(null));

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    error.textContent = '';
    if (note) note.textContent = '';
    const body = Object.fromEntries([...new FormData(form)].filter(([, v]) => v !== ''));
    if (presented) body.fingerprint = presented;
    submit.disabled = true;
    try {
      const response = await request('POST', path, body);
      if (response.status === 201) {
        form.reset();
        showFingerprint(null);
        if (note) note.textContent = onAdded(response.data) ?? '';
        return;
      }
      const data = response.data ?? {};
      if (data.error === 'fingerprint_unconfirmed') {
        showFingerprint(data.fingerprint);
      } else if (data.error === 'fingerprint_mismatch') {
        showFingerprint(data.fingerprint);
        error.textContent = 'The Device now presents a different certificate. Check the new fingerprint before confirming.';
      } else {
        error.textContent = data.message ?? `That failed (HTTP ${response.status}).`;
      }
    } catch (failure) {
      error.textContent = `That failed: ${failure.message}`;
    } finally {
      submit.disabled = false;
    }
  });
}

function skippedNote(device) {
  const skipped = device.skipped_cameras ?? [];
  if (!skipped.length) return '';
  const names = skipped.map((camera) => JSON.stringify(camera.name)).join(', ');
  return `Left out ${skipped.length} Camera${skipped.length === 1 ? '' : 's'} with unsafe names: ${names}.`;
}

export function mountDevices(root, store, { announce }) {
  const list = root.querySelector('.device-list');
  const listError = root.querySelector('[data-device-error]');

  const remove = async (device, button) => {
    const ok = window.confirm(`Remove ${device.label}? Its Cameras disappear for everyone and the instance stops pulling them.`);
    if (!ok) return;
    listError.textContent = '';
    button.disabled = true;
    try {
      const response = await request('DELETE', `/api/devices/${encodeURIComponent(device.label)}`);
      if (!response.ok && response.status !== 404) {
        listError.textContent = response.data?.message ?? `Removing ${device.label} failed (HTTP ${response.status}).`;
      }
    } catch (failure) {
      listError.textContent = `Removing ${device.label} failed: ${failure.message}`;
    } finally {
      button.disabled = false;
    }
  };
  const items = new Map(); // Device label -> its entry, kept across redraws
  const ordered = () => inOrder(store.get('devices') ?? [], store.get('deviceOrder') ?? [], (d) => d.label);
  const render = () => {
    const devices = ordered();
    const labels = new Set(devices.map((d) => d.label));
    for (const [label, item] of items) {
      if (!labels.has(label)) {
        item.remove();
        items.delete(label);
      }
    }
    for (const device of devices) {
      if (!items.has(device.label)) {
        const item = deviceItem(device, remove);
        item.position.show(store.get('fixed_positions')?.[device.label] ?? null);
        item.position.showPosition(store.get('positions')?.[device.label] ?? null);
        items.set(device.label, item);
      }
      items.get(device.label).update(device);
    }
    placeInOrder(list, devices.map((device) => items.get(device.label)));
  };
  store.on('devices', render);
  store.on('deviceOrder', render);
  store.on('fixed_positions', (places) => {
    for (const [label, item] of items) item.position.show(places?.[label] ?? null);
  });
  store.on('positions', (positions) => {
    for (const [label, item] of items) item.position.showPosition(positions?.[label] ?? null);
  });

  reorderable(list, {
    item: 'li', grip: '.item-grip', keys: '.item-grip',
    idOf: (item) => item.dataset.device,
    move: (label, index) => {
      const order = moved(ordered().map((d) => d.label), label, index);
      if (!order) return;
      store.set('deviceOrder', order);
      announce(`${label} moved to position ${order.indexOf(label) + 1} of ${order.length}`);
    },
  });

  trustOnFirstUse(root.querySelector('#add-device'), {
    path: '/api/devices', addressField: 'address', submitLabel: 'add Device',
    onAdded: (device) => [clockNote(device), skippedNote(device)].filter(Boolean).join(' '),
  });
  trustOnFirstUse(root.querySelector('#add-camera'), {
    path: '/api/cameras', addressField: 'url', submitLabel: 'add Camera', onAdded: () => '',
  });
}
