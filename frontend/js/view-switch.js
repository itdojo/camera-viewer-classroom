// The masthead's Multi View | Map View switch, shown at every screen size so the map is one step
// away on a phone too. Each button sets this browser's `view` (view-state.js) and is pressed
// while its View shows; in Single View neither is, since Single View is reached only from a tile,
// the Camera list or a marker.

export function mountViewSwitch(root, store) {
  const buttons = [...root.querySelectorAll('[data-view]')];
  for (const button of buttons) button.addEventListener('click', () => store.set('view', button.dataset.view));
  store.on('view', (view) => {
    for (const button of buttons) button.setAttribute('aria-pressed', String(button.dataset.view === view));
  });
}
