// A tiny keyed store. Shared state (devices, cameras, and later feed status,
// recordings, captures, disk) arrives from the server's event stream; local
// state (the view, its Cameras: see view-state.js) is set by the UI.
// Modules never call each other: they read and watch keys here.

export function createStore() {
  const state = new Map();
  const listeners = new Map();

  return {
    get(key) {
      return state.get(key);
    },
    set(key, value) {
      state.set(key, value);
      for (const listener of listeners.get(key) ?? []) listener(value);
    },
    // Call listener on every change of key, and once now if key has a value.
    on(key, listener) {
      if (!listeners.has(key)) listeners.set(key, []);
      listeners.get(key).push(listener);
      if (state.has(key)) listener(state.get(key));
    },
  };
}
