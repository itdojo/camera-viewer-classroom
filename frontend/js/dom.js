// Small DOM helpers shared by more than one concern.

// A new `tag` element with an optional class and text.
export function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// Whether an Esc keydown is free for the page's own use: no open dialog
// (Full Screen's included) handles it, and it was not typed in a form field.
export function escapeIsFree(event) {
  if (document.querySelector('dialog[open]')) return false;
  return !event.target.closest?.('input, textarea, select');
}
