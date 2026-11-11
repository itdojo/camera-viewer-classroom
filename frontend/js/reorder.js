// Reordering a list of sibling elements: the Management Bar's Panels, and the
// Camera and Device lists inside them. Alt+↑/↓ on an item's key controls, or
// dragging its grip with a mouse or a finger, works out where the item should
// land and calls `move(id, index)` (index 0 = top). `move` owns the order: it
// saves it, re-renders and announces. Nothing here changes the DOM order.
//
// Drag runs on pointer events with the pointer captured on the grip, so the
// moves and the release reach it wherever the pointer goes. The landing index
// is how many of the other items have their middle above the pointer. While
// it moves, `data-dragging` marks the item and `data-drop` ("above" or
// "below") the item it would land beside. Nothing scrolls during a drag:
// keyboard reorder covers far moves, and drags can be done in steps.
//
// Listeners are delegated to the container, so items may come and go, and an
// item belongs to the nearest container only: a Camera inside the Cameras
// Panel never moves the Panel.

export function reorderable(container, { item, grip, keys, idOf, move }) {
  const items = () => [...container.children].filter((element) => element.matches(item));

  // The control matching selector at target, and the item it belongs to,
  // if that item is one of this container's own.
  const hit = (target, selector) => {
    const control = target.closest?.(selector);
    const owner = control?.closest(item);
    return owner?.parentElement === container ? { control, owner } : null;
  };

  let drag = null; // { element, pointerId, index }

  const dropIndex = (element, y) => items()
    .filter((other) => other !== element)
    .filter((other) => {
      const box = other.getBoundingClientRect();
      return box.top + box.height / 2 < y;
    }).length;

  const clearDrop = () => {
    for (const element of items()) element.removeAttribute('data-drop');
  };

  const markDrop = (index) => {
    clearDrop();
    const all = items();
    if (all.indexOf(drag.element) === index) return; // where it already is
    const others = all.filter((other) => other !== drag.element);
    if (index < others.length) others[index].setAttribute('data-drop', 'above');
    else others.at(-1)?.setAttribute('data-drop', 'below');
  };

  const endDrag = (drop) => {
    if (!drag) return;
    const { element, index } = drag;
    drag = null;
    element.removeAttribute('data-dragging');
    clearDrop();
    if (drop) move(idOf(element), index);
  };

  container.addEventListener('pointerdown', (event) => {
    const found = hit(event.target, grip);
    if (!found || drag || !event.isPrimary || event.button !== 0) return;
    event.preventDefault(); // no text selection, no focus change
    found.control.setPointerCapture(event.pointerId);
    drag = { element: found.owner, pointerId: event.pointerId, index: null };
    found.owner.setAttribute('data-dragging', '');
  });
  container.addEventListener('pointermove', (event) => {
    if (drag?.pointerId !== event.pointerId) return;
    drag.index = dropIndex(drag.element, event.clientY);
    markDrop(drag.index);
  });
  container.addEventListener('pointerup', (event) => {
    if (drag?.pointerId !== event.pointerId) return;
    drag.index = dropIndex(drag.element, event.clientY);
    endDrag(true);
  });
  for (const type of ['pointercancel', 'lostpointercapture']) {
    container.addEventListener(type, (event) => {
      if (drag?.pointerId === event.pointerId) endDrag(false);
    });
  }

  container.addEventListener('keydown', (event) => {
    if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const step = { ArrowUp: -1, ArrowDown: 1 }[event.key];
    const found = step && hit(event.target, keys);
    if (!found) return;
    event.preventDefault();
    move(idOf(found.owner), items().indexOf(found.owner) + step);
  });
}

// Put elements in this order as parent's first children, moving only those
// out of place, so an item being dragged stays put (moving it would drop its
// pointer capture). Moving a focused element blurs it (Chromium), so
// whatever had focus gets it back.
export function placeInOrder(parent, elements) {
  const focused = document.activeElement;
  elements.forEach((element, index) => {
    const here = parent.children[index] ?? null;
    if (here !== element) parent.insertBefore(element, here);
  });
  if (parent.contains(focused) && document.activeElement !== focused) focused.focus();
}

// A new order with id moved to index (clamped), or null when it is already there.
export function moved(order, id, index) {
  const to = Math.max(0, Math.min(index, order.length - 1));
  if (order.indexOf(id) === to) return null;
  const next = order.filter((other) => other !== id);
  next.splice(to, 0, id);
  return next;
}
