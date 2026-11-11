// The one server-sent-events stream. Every event carries the whole current
// state of one resource, so each just replaces its key in the store. The
// stream opens with a snapshot of every resource and EventSource reconnects
// on its own, so there is nothing to reconcile.
//
// EventSource reconnects by itself after a network error, but gives up for
// good on an HTTP error such as 401. When it has given up, ask whether the
// session ended; if so, go back to the login wall.

import { request, toLoginWall } from './api.js';

export function connectEvents(store, resources) {
  const source = new EventSource('/api/events');
  for (const name of resources) {
    source.addEventListener(name, (event) => store.set(name, JSON.parse(event.data)));
  }
  source.addEventListener('open', () => store.set('connected', true));
  source.addEventListener('error', () => {
    store.set('connected', false);
    if (source.readyState !== EventSource.CLOSED) return;
    request('GET', '/api/session').then(({ data }) => {
      if (data?.authenticated === false) toLoginWall();
    });
  });
  return source;
}
