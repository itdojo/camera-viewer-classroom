"""Body Limit: no request body bigger than ``MAX_BODY_BYTES`` reaches a route.

Starlette reads a whole body into memory before a route sees it (``Request.body()``, and every
Pydantic form), so without a cap one request could fill the backend's memory and stop every
viewer and Recording control: a WHEP offer from any signed-in browser, or a login attempt from
anyone. ``BodyLimit`` reads each body itself, counting as it goes, and hands the route only a
body within the cap. A bigger one gets 413 ``body_too_large`` (with ``max_bytes``) in the API's
error shape, whether it declared its length or arrived chunked, and the route never runs.

The cap is far above anything the UI sends; an SDP offer is a few kilobytes. The Snapshot
upload is the one route with a limit of its own, ``CV_SNAPSHOT_MAX_BYTES``, which it checks
while it streams (``api/snapshots.py``), so its body passes through untouched.
"""

from __future__ import annotations

import json

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 64 * 1024
OWN_LIMIT = frozenset({"/api/snapshots"})  # routes that bound their own body as they read it


class BodyLimit:
    """ASGI middleware: every request body is read, up to ``max_bytes``, before the app runs."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in OWN_LIMIT:
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            await self._refuse(send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return  # nobody is left to answer
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                await self._refuse(send)
                return
            if not message.get("more_body", False):
                break

        whole: Message | None = {"type": "http.request", "body": bytes(body), "more_body": False}

        async def replay() -> Message:
            # The body once, then the client's own messages: a stream still hears a disconnect.
            nonlocal whole
            if whole is None:
                return await receive()
            message, whole = whole, None
            return message

        await self.app(scope, replay, send)

    async def _refuse(self, send: Send) -> None:
        body = json.dumps({"error": "body_too_large",
                           "message": f"a request body may be at most {self.max_bytes} bytes",
                           "max_bytes": self.max_bytes}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()),
                                (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})
