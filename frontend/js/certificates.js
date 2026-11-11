// Certificate changed: one alert per Device whose rebuilt certificate no
// longer matches its pin, showing the fingerprint it presents now and a
// button that re-pins to exactly that fingerprint. The click is the
// confirmation; nothing re-pins without it. If the Device's certificate has
// moved on again since, the instance answers 409 with the newer fingerprint,
// which replaces the one shown so the student confirms what they see.

import { request } from './api.js';

const colons = (fingerprint) => fingerprint.match(/.{1,2}/g).join(':');

function alertFor(device) {
  const section = document.createElement('section');
  section.className = 'certificate-alert';
  const heading = document.createElement('h3');
  heading.id = `certificate-${device.label}`;
  heading.textContent = `${device.label}: certificate changed`;
  section.setAttribute('aria-labelledby', heading.id);

  const explain = document.createElement('p');
  explain.className = 'hint';
  explain.textContent = 'This Device now presents a different certificate, so its Feeds are refused. '
    + 'If it was rebuilt, check the new fingerprint against what gen-cert.sh printed, then trust it.';
  const caption = document.createElement('p');
  caption.className = 'eyebrow';
  caption.textContent = 'New fingerprint (SHA-256)';
  const value = document.createElement('code');
  value.textContent = colons(device.presented_fingerprint);
  const error = document.createElement('p');
  error.className = 'form-error';
  error.setAttribute('role', 'alert');
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn btn-primary';
  button.textContent = 'Trust new certificate';

  let shown = device.presented_fingerprint;
  button.addEventListener('click', async () => {
    error.textContent = '';
    button.disabled = true;
    try {
      const response = await request('PUT', `/api/devices/${encodeURIComponent(device.label)}/fingerprint`,
        { fingerprint: shown });
      if (response.ok) return; // the devices event removes this alert
      const data = response.data ?? {};
      if (data.error === 'fingerprint_mismatch' && data.fingerprint) {
        shown = data.fingerprint;
        value.textContent = colons(shown);
        error.textContent = 'The certificate changed again. Check this fingerprint before trusting it.';
      } else {
        error.textContent = data.message ?? `Re-pinning failed (HTTP ${response.status}).`;
      }
    } catch (failure) {
      error.textContent = `Re-pinning failed: ${failure.message}`;
    } finally {
      button.disabled = false;
    }
  });

  section.append(heading, explain, caption, value, error, button);
  return section;
}

export function mountCertificateAlerts(root, store) {
  const container = root.querySelector('[data-certificate-alerts]');
  const alerts = new Map(); // label -> {fingerprint, element}; kept across renders so a pending click survives

  store.on('devices', (devices) => {
    const changed = devices.filter((d) => d.certificate_changed && d.presented_fingerprint);
    for (const [label, entry] of alerts) {
      const device = changed.find((d) => d.label === label);
      if (!device || device.presented_fingerprint !== entry.fingerprint) alerts.delete(label);
    }
    for (const device of changed) {
      if (!alerts.has(device.label)) {
        alerts.set(device.label, { fingerprint: device.presented_fingerprint, element: alertFor(device) });
      }
    }
    container.replaceChildren(...[...alerts.values()].map((entry) => entry.element));
    store.set('certificateAlerts', alerts.size); // the Management Bar badges this count
  });
}
