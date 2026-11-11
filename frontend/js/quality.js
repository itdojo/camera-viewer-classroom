// Quality: the gear menu and the "Medium · auto" label on every Feed (ADR 0009).
//
// A Camera's Quality is set at its Device, so it is shared by every browser:
// this module reads only the server's `cameras` state (each Camera carries a
// `quality` object) and changes it with PUT /api/cameras/<id>/quality.
//
// The menu follows the WAI-ARIA menu button pattern: the gear opens a menu of
// menuitemradio items (Auto and the four levels, the current mode checked);
// The gear's name says the current Quality ("Quality: Medium · auto").
// Enter, Space or ↓ opens it on the checked item, ↑/↓ Home/End move, Esc
// closes it and returns to the gear, Tab leaves it. A change that would split
// a Recording comes back from the server as 409 `confirm_required`; the menu
// then shows a confirm step in place of its items, so the warning can't be
// missed and is never a browser dialog. The gear is disabled, with the
// server's reason as its description, for a Camera added by RTSP URL, an
// Offline Device or a changed certificate.

import { cameraPath, request } from './api.js';
import { decorateTiles } from './tile.js';
import { selectedCamera } from './view-state.js';

const LEVELS = { high: 'High', medium: 'Medium', low: 'Low', minimum: 'Minimum' };
const MODES = [['auto', 'Auto'], ...Object.entries(LEVELS)];

let menus = 0; // ids for aria-controls

// "Medium · auto", "Low", "Auto" (on Auto, level not known yet), or '' when
// there is nothing to say (no Quality at all).
export function qualityLabel(quality) {
  if (!quality) return '';
  const level = LEVELS[quality.level] ?? null;
  if (!quality.available && !level) return '';
  if (quality.mode === 'auto') return level ? `${level} · auto` : 'Auto';
  return level ?? LEVELS[quality.mode] ?? '';
}

export async function setQuality(cameraId, mode, confirm = false) {
  return request('PUT', `/api/cameras/${cameraPath(cameraId)}/quality`, { mode, confirm });
}

// The label, the "changed by another instance" notice, the gear and its menu,
// and a status line for a refused change, for one Camera at a time. Call
// update(camera) on every change of that Camera; the elements are built once,
// so focus and an open menu survive the re-render a change causes.
export function qualityControl() {
  const id = `quality-menu-${++menus}`;
  const element = document.createElement('span');
  element.className = 'quality-control';

  const label = document.createElement('span');
  label.className = 'quality-label';
  label.setAttribute('aria-hidden', 'true'); // the gear's name carries it: "Quality: Medium · auto"
  const notice = document.createElement('span');
  notice.className = 'quality-notice';
  notice.textContent = 'Changed by another instance';
  notice.title = 'Another instance changed this Camera\'s Quality. Pick a level, or Auto, to take control back.';
  notice.hidden = true;

  const gear = document.createElement('button');
  gear.type = 'button';
  gear.className = 'btn btn-quality';
  gear.setAttribute('aria-label', 'Quality');
  gear.setAttribute('aria-haspopup', 'menu');
  gear.setAttribute('aria-expanded', 'false');
  gear.setAttribute('aria-controls', id);
  const icon = document.createElement('span');
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = '⚙';
  gear.append(icon);
  const why = document.createElement('span');
  why.className = 'visually-hidden';
  why.id = `${id}-why`;

  const menu = document.createElement('div');
  menu.className = 'quality-menu';
  menu.id = id;
  menu.dataset.ownsEscape = ''; // the Management Bar leaves Esc to it (management-bar.js)
  menu.hidden = true;
  const items = document.createElement('div');
  items.setAttribute('role', 'menu');
  items.setAttribute('aria-label', 'Quality');
  const options = MODES.map(([mode, text]) => {
    const option = document.createElement('button');
    option.type = 'button';
    option.className = 'quality-option';
    option.setAttribute('role', 'menuitemradio');
    option.dataset.mode = mode;
    option.textContent = text;
    option.tabIndex = -1;
    option.addEventListener('click', () => choose(mode));
    items.append(option);
    return option;
  });

  const confirmStep = document.createElement('div');
  confirmStep.className = 'quality-confirm';
  confirmStep.setAttribute('role', 'group');
  confirmStep.setAttribute('aria-labelledby', `${id}-warning`);
  confirmStep.hidden = true;
  const warning = document.createElement('p');
  warning.id = `${id}-warning`;
  warning.textContent = 'This splits the Recording: viewers lose the picture for a few seconds, '
    + 'and the Recording goes on in a new part.';
  const split = document.createElement('button');
  split.type = 'button';
  split.className = 'btn btn-primary';
  split.textContent = 'Change and split';
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'btn btn-quiet';
  cancel.textContent = 'Cancel';
  confirmStep.append(warning, split, cancel);
  menu.append(items, confirmStep);

  const status = document.createElement('span');
  status.className = 'quality-status';
  status.setAttribute('role', 'status');

  element.append(label, notice, gear, why, menu, status);

  let camera = null;
  let busy = false;
  let pending = null; // the mode waiting for the Recording confirm
  let failure = ''; // a refusal from this browser's last request

  const quality = () => camera?.quality ?? null;
  const isOpen = () => !menu.hidden;

  const render = () => {
    const q = quality();
    element.hidden = !camera || !q;
    if (!q) return;
    label.textContent = qualityLabel(q);
    gear.setAttribute('aria-label', label.textContent ? `Quality: ${label.textContent}` : 'Quality');
    notice.hidden = !q.changed_elsewhere;
    gear.disabled = busy || !q.available;
    if (q.available) {
      gear.removeAttribute('aria-describedby');
      gear.title = 'Quality';
      why.textContent = '';
    } else {
      why.textContent = `Quality can't be changed: ${q.reason}`;
      gear.setAttribute('aria-describedby', why.id);
      gear.title = why.textContent;
    }
    for (const option of options) option.setAttribute('aria-checked', String(option.dataset.mode === q.mode));
    const message = failure || q.error?.message || '';
    status.textContent = message;
    status.title = message;
    if (!q.available && isOpen()) close(false);
  };

  const focusOption = (index) => {
    const n = options.length;
    options[((index % n) + n) % n].focus();
  };

  const showItems = () => {
    pending = null;
    confirmStep.hidden = true;
    items.hidden = false;
  };

  // A press anywhere else closes the menu; listened for only while it is open.
  const outside = (event) => {
    if (!element.contains(event.target)) close(false);
  };

  const open = (focus = 'checked') => {
    if (gear.disabled) return;
    showItems();
    menu.hidden = false;
    gear.setAttribute('aria-expanded', 'true');
    document.addEventListener('pointerdown', outside);
    const checked = options.findIndex((option) => option.getAttribute('aria-checked') === 'true');
    focusOption(focus === 'last' ? options.length - 1 : Math.max(checked, 0));
  };

  function close(returnFocus = true) {
    document.removeEventListener('pointerdown', outside);
    menu.hidden = true;
    showItems();
    gear.setAttribute('aria-expanded', 'false');
    if (returnFocus) gear.focus();
  }

  async function choose(mode, confirm = false) {
    if (!camera || busy) return;
    if (!confirm && mode === quality()?.mode && !quality()?.changed_elsewhere) {
      close();
      return;
    }
    busy = true;
    failure = '';
    render();
    try {
      const { ok, status: code, data } = await setQuality(camera.id, mode, confirm);
      if (code === 409 && data?.error === 'confirm_required') {
        pending = mode;
        items.hidden = true;
        confirmStep.hidden = false;
        split.focus();
        return;
      }
      failure = ok ? '' : (data?.message ?? 'The Quality did not change.');
      close();
    } finally {
      busy = false;
      render();
    }
  }

  gear.addEventListener('click', () => (isOpen() ? close() : open()));
  gear.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      open(event.key === 'ArrowUp' ? 'last' : 'checked');
    }
  });
  menu.addEventListener('keydown', (event) => {
    const index = options.indexOf(document.activeElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation(); // Esc closes the menu, not Single View or the Management Bar
      close();
    } else if (event.key === 'Tab') {
      close(false);
    } else if (index >= 0 && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      if (event.key === 'ArrowDown') focusOption(index + 1);
      else if (event.key === 'ArrowUp') focusOption(index - 1);
      else focusOption(event.key === 'Home' ? 0 : options.length - 1);
    }
  });
  split.addEventListener('click', () => choose(pending, true));
  cancel.addEventListener('click', () => close());

  render();
  return {
    element,
    update(next) {
      if (next?.id !== camera?.id) {
        failure = ''; // a refusal belongs to the Camera it was for
        if (isOpen()) close(false);
      }
      camera = next ?? null;
      render();
    },
  };
}

// Multi View tiles: the control in each tile's `controls` slot (clicks there
// never open Single View). Recording fills that slot too, with replaceChildren
// when the tile is created, so the control re-appends itself whenever it
// finds itself missing, whatever order the decorators were registered in.
export function mountQualityOnTiles() {
  const controls = new WeakMap(); // tile element -> its control
  decorateTiles(({ slots, camera, element }) => {
    if (!controls.has(element)) controls.set(element, qualityControl());
    const control = controls.get(element);
    control.update(camera);
    const place = () => {
      if (!slots.controls.contains(control.element)) slots.controls.prepend(control.element);
    };
    place();
    queueMicrotask(place);
  });
}

// Single View: the control in the header, for the selected Camera.
export function mountQualityOnSingleView(root, store) {
  const control = qualityControl();
  root.querySelector('[data-feed-status]').after(control.element);
  const update = () => control.update(selectedCamera(store) ?? null);
  store.on('cameras', update);
  store.on('selected', update);
}
