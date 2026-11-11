// The disk indicator in the masthead: free and total space for the captures
// folder, from the server's `disk` event (pushed after every measurement),
// and what the instance is doing about it once space runs low. The levels
// and their thresholds are the server's; this only says them.

import { size } from './format.js';

function alertText(disk) {
  const t = disk.thresholds;
  switch (disk.level) {
    case 'warn':
      return 'Low on space. Delete Captures you no longer need.';
    case 'refuse':
      return `New Recordings and Snapshots are refused below ${size(t.refuse_bytes)} free.`;
    case 'finalize':
      return `Under ${size(t.finalize_bytes)} free: active Recordings are stopped and saved.`
        + (disk.refusing ? ' New Recordings and Snapshots are refused.' : '');
    default:
      return '';
  }
}

export function mountDiskIndicator(root, store) {
  const text = root.querySelector('[data-disk-text]');
  const used = root.querySelector('[data-disk-used]');
  const alert = root.querySelector('[data-disk-alert]');

  store.on('disk', (disk) => {
    root.hidden = false;
    root.dataset.level = disk.level;
    text.textContent = `${size(disk.free)} free of ${size(disk.total)}`;
    const percent = disk.total ? Math.round(((disk.total - disk.free) / disk.total) * 100) : 0;
    used.style.width = `${percent}%`;
    root.title = `${percent}% of the captures disk is in use`;
    alert.textContent = alertText(disk);
  });
}
