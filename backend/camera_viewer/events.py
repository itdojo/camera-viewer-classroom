"""The event bus behind the one server-sent-events stream.

Shared state reaches browsers as named events whose data is the resource's
whole current state (``cameras`` carries the full Camera list, ``devices``
the full Device list). Whole-state events are idempotent, so a browser never
has to reconcile deltas, and a reconnect is just a fresh snapshot.

A module that owns a resource registers a snapshot provider under the event
name; every new subscriber first receives one event per registered snapshot,
then live events as they are published. ``publish`` is synchronous and must
be called on the event loop's thread.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

Snapshot = Callable[[], Any]

KEEPALIVE_SECONDS = 15.0


class EventBus:
    def __init__(self, queue_size: int = 256) -> None:
        self._queue_size = queue_size
        self._snapshots: dict[str, Snapshot] = {}
        self._subscribers: set[asyncio.Queue] = set()

    def register_snapshot(self, event: str, snapshot: Snapshot) -> None:
        self._snapshots[event] = snapshot

    def publish(self, event: str, data: Any) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait((event, data))
            except asyncio.QueueFull:
                # A subscriber this far behind is dropped; its browser
                # reconnects and starts again from a snapshot.
                self._subscribers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)

    async def stream(self) -> AsyncIterator[str]:
        """One subscription, encoded as text/event-stream: a snapshot event per
        registered resource, then live events, with keepalive comments."""
        queue: asyncio.Queue = asyncio.Queue(self._queue_size)
        self._subscribers.add(queue)
        try:
            yield "retry: 2000\n\n"
            for event, snapshot in self._snapshots.items():
                yield _encode(event, snapshot())
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    return
                yield _encode(*item)
        finally:
            self._subscribers.discard(queue)


def _encode(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
