// The Management Bar: the slide-over that holds the Camera, Capture and
// Device lists and the add forms. It starts closed so the Feeds get the whole
// window, and opens over the left edge of the viewer without re-laying it out
// (see app.css). The toggle in the masthead, `\` and Esc open and close it.
// At 760px and under the bar is stacked below the viewer and always shown,
// so the toggle is hidden and the shortcuts do nothing.
//
// Inside it, each Panel (a <section data-panel="<id>"> in index.html)
// collapses and expands from its header button, at any width. A collapsed
// Panel's body is `hidden`, so it leaves the accessibility tree and the tab
// order. Every Panel starts expanded. Alt+↑/↓ on its header or its grip (a
// button named "Move <Panel>"), or dragging the grip with a mouse or a
// finger, moves a Panel, also at any width, and Reset layout restores the
// default order and expands every Panel.
//
// The layout belongs to this browser alone, like view-state.js: it is
// mirrored to localStorage under its own key as `{ open, order, collapsed }`,
// where `order` lists every Panel id top to bottom and `collapsed` the ids
// of the collapsed ones. Ids the page no longer has are dropped on load, and
// Panels missing from a saved order go back to their default place.
// Storage can be missing or throw (private windows, blocked site data); the
// bar then starts from defaults.
//
// Certificate alerts (counted in the store by certificates.js) badge the
// Devices Panel header while that Panel is collapsed, and the toggle while
// the bar is closed, so an alert is noticed without anything springing open.

import { moved, placeInOrder, reorderable } from './reorder.js';

const STORAGE_KEY = 'camera-viewer:management-bar';
const NARROW = '(max-width: 760px)';

// A saved order from an older or newer page: ids the page no longer has are
// dropped, and a Panel the save never knew goes back to its default index,
// so a stale layout never hides a Panel.
function reconcile(saved, ids) {
  if (!Array.isArray(saved)) return ids;
  const order = [...new Set(saved)].filter((id) => ids.includes(id));
  ids.forEach((id, index) => {
    if (!order.includes(id)) order.splice(index, 0, id);
  });
  return order;
}

function load(ids) {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}');
    const collapsed = Array.isArray(saved?.collapsed) ? saved.collapsed : [];
    const order = reconcile(saved?.order, ids);
    return {
      open: saved?.open === true,
      order,
      collapsed: ids.filter((id) => collapsed.includes(id)),
    };
  } catch {
    return { open: false, order: ids, collapsed: [] };
  }
}

function save(state) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // storage unavailable: the bar still works for this page's lifetime
  }
}

export function mountManagementBar(bar, toggle, store) {
  const panels = [...bar.querySelectorAll('[data-panel]')].map((element) => ({
    id: element.dataset.panel,
    element,
    header: element.querySelector('.panel-toggle'),
    name: element.querySelector('.panel-toggle').textContent.trim(), // before anything decorates the header
    body: element.querySelector('.panel-body'),
  }));
  const byId = new Map(panels.map((panel) => [panel.id, panel]));
  const liveRegion = bar.querySelector('[data-moved]'); // polite: announces a moved Panel, Camera or Device
  const state = load(panels.map((panel) => panel.id));
  const narrow = window.matchMedia(NARROW);

  // Order is the order of the <section>s in the DOM, not CSS `order`, so
  // focus on open, the tab order and a screen reader all follow what is on
  // screen.
  const placePanels = () => placeInOrder(bar, state.order.map((id) => byId.get(id).element));

  // Each badge sits beside its control and describes it (aria-describedby):
  // inside the button, its text would join the heading, region and form
  // names the Panel header lends, and the toggle's exact name.
  const badges = [
    { control: toggle, element: document.getElementById('bar-alert-count'), shown: () => !state.open },
    { control: byId.get('devices').header, element: document.getElementById('devices-alert-count'), shown: () => state.collapsed.includes('devices') },
  ];

  const renderAlertBadges = () => {
    const count = store.get('certificateAlerts') ?? 0;
    for (const { control, shown, element } of badges) {
      const show = count > 0 && shown();
      element.hidden = !show;
      if (show) {
        const words = document.createElement('span');
        words.className = 'visually-hidden';
        words.textContent = count === 1 ? ' certificate alert' : ' certificate alerts';
        element.replaceChildren(String(count), words);
        control.setAttribute('aria-describedby', element.id);
      } else {
        element.replaceChildren();
        control.removeAttribute('aria-describedby');
      }
    }
  };

  const render = () => {
    bar.toggleAttribute('data-open', state.open);
    toggle.setAttribute('aria-expanded', String(state.open));
    for (const panel of panels) {
      const collapsed = state.collapsed.includes(panel.id);
      panel.header.setAttribute('aria-expanded', String(!collapsed));
      panel.body.hidden = collapsed;
    }
    placePanels();
    renderAlertBadges();
  };

  // The one way a Panel moves: Alt+↑/↓ on its header or grip and a grip drag
  // (reorder.js) all call this with the Panel's id and the index (0 = top) it
  // should end at.
  const movePanel = (id, index) => {
    const order = moved(state.order, id, index);
    if (!order) return;
    state.order = order;
    save(state);
    render();
    liveRegion.textContent = `${byId.get(id).name} moved to position ${order.indexOf(id) + 1} of ${order.length}`;
  };
  reorderable(bar, {
    item: '[data-panel]', grip: '.panel-grip', keys: '.panel-toggle, .panel-grip',
    idOf: (element) => element.dataset.panel, move: movePanel,
  });

  // Opening by hand moves focus to the first Panel header. Closing returns
  // it to the toggle only if it was in the bar or on the toggle, so a
  // keyboard user never loses their place and focus elsewhere stays put.
  const setOpen = (open) => {
    const hadFocus = bar.contains(document.activeElement) || document.activeElement === toggle;
    state.open = open;
    save(state);
    render();
    if (open) bar.querySelector('[data-panel] .panel-toggle')?.focus();
    else if (hadFocus) toggle.focus();
  };

  const setCollapsed = (id, collapsed) => {
    state.collapsed = collapsed ? [...state.collapsed, id] : state.collapsed.filter((other) => other !== id);
    save(state);
    render();
  };

  // Back to the default order with every Panel expanded; the bar stays open.
  const resetLayout = () => {
    state.order = panels.map((panel) => panel.id);
    state.collapsed = [];
    save(state);
    render();
  };

  toggle.addEventListener('click', () => setOpen(!state.open));
  bar.querySelector('[data-reset-layout]').addEventListener('click', resetLayout);
  for (const panel of panels) {
    panel.header.addEventListener('click', () => setCollapsed(panel.id, !state.collapsed.includes(panel.id)));
  }

  // Capture phase, so Esc reaches this handler before Single View's (in
  // view-state.js) and, when it closes the bar, goes no further: one keypress
  // never closes the bar and leaves Single View too.
  document.addEventListener('keydown', (event) => {
    if (narrow.matches || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === '\\') {
      if (event.target.closest?.('input, textarea, select')) return; // typing a backslash
      event.preventDefault();
      setOpen(!state.open);
    } else if (event.key === 'Escape' && state.open) {
      if (document.querySelector('dialog[open]')) return; // Esc is the dialog's
      if (event.target.closest?.('[data-owns-escape]')) return; // an open popup's, such as the Quality menu
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
    }
  }, { capture: true });

  // Widening past 760px hides a closed bar (visibility: hidden), which drops
  // focus inside it to <body>; the toggle that reopens it takes focus. With no
  // slide to hold the bar visible (reduced motion, or before data-ready) focus
  // is already on <body> when the change is reported, so the bar's last focus
  // is remembered: true while the most recent focusin was inside it.
  let barHadFocus = false;
  document.addEventListener('focusin', (event) => {
    barHadFocus = bar.contains(event.target);
  });
  narrow.addEventListener('change', () => {
    if (narrow.matches || state.open) return;
    const active = document.activeElement;
    const dropped = (active === null || active === document.body) && barHadFocus;
    if (bar.contains(active) || dropped) toggle.focus();
  });

  store.on('certificateAlerts', renderAlertBadges);

  // A bar saved open is simply there on load; only a hand toggle slides.
  render();
  requestAnimationFrame(() => requestAnimationFrame(() => bar.setAttribute('data-ready', '')));
}
