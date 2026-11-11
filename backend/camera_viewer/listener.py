"""The UI's listener: uvicorn serving the backend, over HTTPS by default (ADR 0014).

With TLS on, one port answers both ways. Each connection's first byte decides:
a TLS handshake record (0x16) is upgraded in place and handed to uvicorn's own
HTTP protocol, so the request keeps the client's address and the ``https``
scheme; anything else is read as a plain HTTP request and answered with a 301
to the same host and path over ``https://``, then closed. A browser pointed
at ``http://`` therefore lands on the right page instead of an empty reply.
A connection that sends nothing, or no complete request head, within
``FIRST_REQUEST_SECONDS`` is closed. Every connection closed before uvicorn
takes it over is logged with its peer and the reason, as is one that ends in
an error afterwards, so a client-side TLS error can be matched to what the
listener saw.

Run as ``python -m camera_viewer.listener``; the supervisor starts it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import ssl

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from camera_viewer.config import Settings

TLS_HANDSHAKE = 0x16
FIRST_REQUEST_SECONDS = 10.0
MAX_PLAIN_HEAD = 8192
HOST = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](:\d{1,5})?$")

log = logging.getLogger("camera_viewer.listener")


def server_context(cert: str, key: str) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


class HttpsOrRedirect(asyncio.Protocol):
    """Upgrades a TLS connection for uvicorn, or redirects a plain HTTP one."""

    context: ssl.SSLContext  # set by ``https_protocol``
    first_request_seconds = FIRST_REQUEST_SECONDS

    def __init__(self, config, server_state, app_state, _loop=None):
        self._uvicorn = {"config": config, "server_state": server_state, "app_state": app_state, "_loop": _loop}
        self._loop = _loop or asyncio.get_event_loop()
        self._head = b""
        self._peek: socket.socket | None = None

    def connection_made(self, transport: asyncio.Transport) -> None:
        self._transport = transport
        # Nothing is read until the first byte says what this is. The descriptor
        # is watched through a duplicate, since asyncio won't watch one its
        # transport owns.
        transport.pause_reading()
        raw = transport.get_extra_info("socket")
        self._peek = socket.fromfd(raw.fileno(), raw.family, raw.type)
        self._peek.setblocking(False)
        self._loop.add_reader(self._peek.fileno(), self._first_byte)
        self._deadline = self._loop.call_later(self.first_request_seconds, self._close,
                                               f"nothing usable within {self.first_request_seconds:g}s")

    def connection_lost(self, exc: Exception | None) -> None:
        if not self._deadline.cancelled() and exc is not None:
            self._note(f"lost before its first request: {exc!r}")
        self._deadline.cancel()
        self._stop_peeking()

    def _note(self, reason: str) -> None:
        log.info("closed a connection from %s: %s", self._transport.get_extra_info("peername"), reason)

    def _close(self, reason: str) -> None:
        self._note(reason)
        self._deadline.cancel()
        self._transport.close()

    def _stop_peeking(self) -> None:
        if self._peek is not None:
            self._loop.remove_reader(self._peek.fileno())
            self._peek.close()
            self._peek = None

    def _first_byte(self) -> None:
        try:
            first = self._peek.recv(1, socket.MSG_PEEK)
        except BlockingIOError:
            return
        except OSError as exc:
            self._note(f"peek failed: {exc!r}")
            first = b""
        self._stop_peeking()
        if not first:
            self._note("closed before sending anything")
        if first and first[0] == TLS_HANDSHAKE:
            self._deadline.cancel()
            self._loop.create_task(self._upgrade())
        else:
            self._transport.resume_reading()

    async def _upgrade(self) -> None:
        # start_tls can deliver the first request before this coroutine resumes,
        # so uvicorn's protocol is attached only once it has its transport, and
        # whatever arrived in between is replayed to it.
        handoff = _Handoff()
        try:
            tls = await self._loop.start_tls(self._transport, handoff, self.context, server_side=True,
                                             ssl_handshake_timeout=self.first_request_seconds)
        except (ssl.SSLError, OSError, ConnectionError, TimeoutError) as exc:
            self._note(f"TLS handshake failed: {exc!r}")
            self._transport.close()
            return
        if handoff.lost:
            self._note(f"lost during the TLS handshake: {handoff.exc!r}")
            return
        protocol = _Logged(**self._uvicorn)
        tls.set_protocol(protocol)
        protocol.connection_made(tls)
        for data in handoff.received:
            protocol.data_received(data)
        if handoff.eof:
            protocol.eof_received()

    def data_received(self, data: bytes) -> None:
        self._head += data
        head, complete, _ = self._head.partition(b"\r\n\r\n")
        if not complete:
            if len(self._head) > MAX_PLAIN_HEAD:
                self._close(f"plain request head over {MAX_PLAIN_HEAD} bytes")
            return
        self._deadline.cancel()
        location = self._location(head.decode("latin-1").split("\r\n"))
        if location is None:
            self._close(f"not an HTTP request: {head[:40]!r}")
            return
        body = f"This instance is served over HTTPS: {location}\n".encode("latin-1")
        self._transport.write(
            b"HTTP/1.1 301 Moved Permanently\r\n"
            + f"Location: {location}\r\n".encode("latin-1")
            + b"Content-Type: text/plain; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("latin-1")
            + b"Connection: close\r\n\r\n" + body)
        self._transport.close()

    def _location(self, lines: list[str]) -> str | None:
        """Where a plain request should go, or None if it isn't an HTTP request."""
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/"):
            return None
        target = parts[1] if parts[1].startswith("/") else "/"
        host = next((line.split(":", 1)[1].strip() for line in lines[1:]
                     if line.lower().startswith("host:")), "")
        if not HOST.match(host):
            address, port = self._transport.get_extra_info("sockname")[:2]
            host = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
        return f"https://{host}{target}"


class _Logged(H11Protocol):
    """uvicorn's HTTP protocol, noting a connection that ends in an error rather than a close."""

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None:
            log.info("lost a connection from %s mid-request: %r", self.client, exc)
        super().connection_lost(exc)


class _Handoff(asyncio.Protocol):
    """Holds what a freshly upgraded connection delivers until uvicorn's protocol takes over."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.eof = False
        self.lost = False
        self.exc: Exception | None = None

    def data_received(self, data: bytes) -> None:
        self.received.append(data)

    def eof_received(self) -> bool:
        self.eof = True
        return True

    def connection_lost(self, exc: Exception | None) -> None:
        self.lost = True
        self.exc = exc


def https_protocol(context: ssl.SSLContext) -> type[HttpsOrRedirect]:
    return type("HttpsOrRedirect", (HttpsOrRedirect,), {"context": context})


class Server(uvicorn.Server):
    def _log_started_message(self, listeners) -> None:
        # uvicorn would call this listener http://; the supervisor logs the real URL.
        pass


def main() -> None:
    settings = Settings.from_env()
    if settings.tls:
        http = https_protocol(server_context(str(settings.tls_cert), str(settings.tls_key)))
    else:
        http = "auto"
    config = uvicorn.Config("camera_viewer.app:create_app", factory=True, host=settings.http_host,
                            port=settings.http_port, access_log=False, http=http)
    (Server(config) if settings.tls else uvicorn.Server(config)).run()


if __name__ == "__main__":
    main()
