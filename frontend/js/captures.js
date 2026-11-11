// Captures list: every Recording and Snapshot in the instance's captures
// folder, newest first, as the server pushes it (the `captures` event), so a
// Capture made from any browser shows up in every other without a reload.
//
// Each row offers Preview (a dialog that plays the Recording or shows the
// Snapshot), Download (the original file, byte for byte) and Delete (a trash
// icon at the row's top right), which asks first in its own dialog. A file of a Recording still running can't
// be deleted; a Recording cut off by a restart is flagged Interrupted.

import { request } from './api.js';
import { element } from './dom.js';
import { size } from './format.js';

const KIND_LABEL = { recording: 'Recording', snapshot: 'Snapshot' };

function captureUrl(name, { download = false } = {}) {
  return `/api/captures/${encodeURIComponent(name)}${download ? '?download=1' : ''}`;
}

function formatTime(iso) {
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? '' : when.toLocaleString();
}

function badge(text, state) {
  const node = element('span', 'badge badge-capture', text);
  node.dataset.state = state;
  return node;
}

// One <dialog> for previews, reused: a <video> for a Recording, an <img> for a Snapshot.
function previewDialog() {
  const dialog = element('dialog', 'capture-dialog capture-preview');
  dialog.setAttribute('aria-labelledby', 'capture-preview-title');
  const header = element('header', 'capture-dialog-header');
  const title = element('h2', 'capture-dialog-title');
  title.id = 'capture-preview-title';
  const close = element('button', 'btn btn-ghost', 'Close');
  close.type = 'button';
  close.addEventListener('click', () => dialog.close());
  header.append(title, close);
  const stage = element('div', 'capture-preview-stage');
  dialog.append(header, stage);
  // Stop playback and the download behind it as soon as the dialog closes.
  dialog.addEventListener('close', () => stage.replaceChildren());
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close(); // a click on the backdrop
  });
  document.body.append(dialog);

  return (capture) => {
    title.textContent = capture.name;
    let media;
    if (capture.kind === 'recording') {
      media = element('video');
      media.controls = true;
      media.autoplay = true;
      media.muted = true;
      media.playsInline = true;
      media.preload = 'metadata';
    } else {
      media = element('img');
      media.alt = `Snapshot ${capture.name}`;
    }
    media.src = captureUrl(capture.name);
    stage.replaceChildren(media);
    dialog.showModal();
  };
}

// One <dialog> that asks before deleting; resolves when the student has answered.
function deleteDialog() {
  const dialog = element('dialog', 'capture-dialog capture-confirm');
  dialog.setAttribute('role', 'alertdialog');
  dialog.setAttribute('aria-labelledby', 'capture-confirm-title');
  dialog.setAttribute('aria-describedby', 'capture-confirm-body');
  const title = element('h2', 'capture-dialog-title');
  title.id = 'capture-confirm-title';
  const body = element('p', 'capture-confirm-body',
    "This removes the file from the host's captures folder for everyone. It can't be undone.");
  body.id = 'capture-confirm-body';
  const error = element('p', 'form-error');
  error.setAttribute('role', 'alert');
  const actions = element('div', 'capture-dialog-actions');
  const cancel = element('button', 'btn btn-ghost', 'Cancel');
  cancel.type = 'button';
  const confirm = element('button', 'btn btn-danger', 'Delete');
  confirm.type = 'button';
  actions.append(cancel, confirm);
  dialog.append(title, body, error, actions);
  document.body.append(dialog);

  let pending = null;
  cancel.addEventListener('click', () => dialog.close());
  confirm.addEventListener('click', async () => {
    if (!pending) return;
    confirm.disabled = true;
    error.textContent = '';
    const { ok, status, data } = await request('DELETE', captureUrl(pending));
    confirm.disabled = false;
    if (ok || status === 404) {
      dialog.close(); // gone either way; the list updates from the server
    } else {
      error.textContent = data?.message ?? `The instance answered ${status}.`;
    }
  });
  dialog.addEventListener('close', () => {
    pending = null;
    error.textContent = '';
  });

  return (capture) => {
    pending = capture.name;
    title.textContent = `Delete ${capture.name}?`;
    dialog.showModal();
    cancel.focus(); // the safe answer is the default
  };
}

const SVG = 'http://www.w3.org/2000/svg';

// A trash can, drawn in the button's own colour at the caption's size.
function trashIcon() {
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.classList.add('icon');
  for (const d of ['M2.5 4h11', 'M6 4V2.5h4V4', 'M3.75 4l.75 9.5h7l.75-9.5', 'M6.5 6.5v4.5', 'M9.5 6.5v4.5']) {
    const path = document.createElementNS(SVG, 'path');
    path.setAttribute('d', d);
    svg.append(path);
  }
  return svg;
}

function captureRow(capture, { preview, askDelete }) {
  const item = element('li', 'capture');
  item.dataset.name = capture.name;

  const name = element('span', 'capture-name', capture.name);
  const meta = element('p', 'meta capture-meta');
  const flags = element('span', 'capture-flags');
  const actions = element('div', 'capture-actions');

  const previewButton = element('button', 'btn btn-ghost', 'Preview');
  previewButton.type = 'button';
  const download = element('a', 'btn btn-ghost', 'Download');
  // Delete sits where a Device's Remove does: top right of the entry, a trash
  // icon so a long file name keeps the line.
  const deleteButton = element('button', 'btn btn-danger capture-delete');
  deleteButton.type = 'button';
  deleteButton.setAttribute('aria-label', 'Delete');
  deleteButton.append(trashIcon());
  actions.append(previewButton, download);
  const head = element('div', 'capture-head');
  head.append(name, deleteButton);
  item.append(head, meta, flags, actions);

  let current = capture;
  previewButton.addEventListener('click', () => preview(current));
  deleteButton.addEventListener('click', () => askDelete(current));

  item.update = (next) => {
    current = next;
    meta.textContent = [KIND_LABEL[next.kind] ?? next.kind, size(next.size), formatTime(next.modified)]
      .filter(Boolean).join(' · ');
    const shown = [];
    if (next.interrupted) shown.push(badge('Interrupted', 'interrupted'));
    if (next.in_progress) shown.push(badge('Recording', 'in-progress'));
    flags.replaceChildren(...shown);
    flags.hidden = shown.length === 0;
    download.href = captureUrl(next.name, { download: true });
    download.setAttribute('download', next.name);
    deleteButton.disabled = next.in_progress;
    deleteButton.title = next.in_progress ? 'Stop the Recording before deleting its files' : 'Delete';
  };
  item.update(capture);
  return item;
}

export function mountCaptures(root, store) {
  const list = root.querySelector('.capture-list');
  const empty = root.querySelector('[data-empty]');
  const handlers = { preview: previewDialog(), askDelete: deleteDialog() };
  const rows = new Map(); // name -> <li>, reused so focus survives an update

  store.on('captures', (captures) => {
    const wanted = [];
    for (const capture of captures ?? []) {
      let row = rows.get(capture.name);
      if (row) row.update(capture);
      else rows.set(capture.name, (row = captureRow(capture, handlers)));
      wanted.push(row);
    }
    const keep = new Set(wanted);
    for (const [name, row] of rows) {
      if (!keep.has(row)) rows.delete(name);
    }
    const unchanged = wanted.length === list.children.length && wanted.every((row, i) => list.children[i] === row);
    if (!unchanged) list.replaceChildren(...wanted);
    empty.hidden = wanted.length > 0;
  });
}
