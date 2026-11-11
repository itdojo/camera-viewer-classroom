"""Device Client: talks to one Device's MediaMTX API over HTTPS.

Two modes. Before a Device is trusted, ``presented_fingerprint()`` reads the
certificate it presents, without trusting it and without sending
credentials, so the student can compare it with the one ``gen-cert.sh``
printed. Once a fingerprint is pinned, every API call checks the TLS peer's
certificate against it during the handshake, before any request (and so any
credential) is sent; a different certificate raises ``FingerprintMismatch``,
never a generic failure.

The Device Kit uses one certificate for the API and RTSPS, so the fingerprint
pinned here is also the Relay's ``sourceFingerprint``.

Quality (ADR 0009) adds three calls: a path's config (``path_config``), a
PATCH of it (``patch_path_config``) and when the Device's MediaMTX started
(``started``). A refusal comes back as a typed reason the UI can name:
``CredentialsRejected`` (401), ``PermissionDenied`` (403), ``DeviceRejected``
(any other answer, with the Device's own error text) or ``DeviceUnreachable``.

Every API answer carries the Device's clock in its ``Date`` header, which is
how the instance learns a Device's clock skew (the Clock Watch).

``stream()`` opens the same pinned, credentialed HTTPS to another port on the
Device's own host (the GPS Sidecar's, #53) and yields the body as it arrives.
API calls and streams share ``_pinned``, so the pin is checked in one place.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import ssl
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpcore

if TYPE_CHECKING:  # registry needs nothing from here, but keep the import graph one-way
    from camera_viewer.registry import Device

API_PORT = 9997
DEFAULT_RTSPS_PORT = 8322
GPS_SIDECAR_PORT = 9998  # the Device Kit's GPS Sidecar (ADR 0010)
API_SERVICE = "the Device API"  # what errors call the API; other ports are named by _service


class DeviceError(Exception):
    """The Device could not be used. ``code`` is stable and machine-readable."""

    code = "device_error"


class DeviceUnreachable(DeviceError):
    code = "device_unreachable"


class CredentialsRejected(DeviceError):
    code = "device_credentials_rejected"


class PermissionDenied(DeviceError):
    """The Device knows the user, but its permissions don't cover the call."""

    code = "device_forbidden"


class DeviceRejected(DeviceError):
    """The Device answered, but not with success: an unknown path, a field it refuses, a body
    it can't read. The message carries the Device's own error text."""

    code = "device_rejected"

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class FingerprintMismatch(DeviceError):
    code = "fingerprint_mismatch"

    def __init__(self, expected: str, presented: str):
        super().__init__(f"the Device presented certificate {presented}, not the pinned {expected}")
        self.expected = expected
        self.presented = presented


@dataclass(frozen=True)
class DiscoveredCamera:
    """A Camera as the Device reported it. ``name`` is exactly what the Device
    sent, possibly not even a string: the caller decides whether it is safe."""

    name: Any
    width: int | None
    height: int | None


@dataclass(frozen=True)
class Discovery:
    cameras: tuple[DiscoveredCamera, ...]
    rtsps_port: int
    clock_skew: float | None = None  # see DeviceClient.clock_skew


def normalize_fingerprint(value: str) -> str:
    """Accept the forms students paste: any case, with or without colons or spaces."""
    return value.replace(":", "").replace(" ", "").strip().lower()


def split_address(address: str) -> tuple[str, int]:
    """``host``, ``host:port``, ``[v6]`` or ``[v6]:port`` → (host, API port)."""
    address = address.strip()
    if address.startswith("["):
        host, _, rest = address[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else API_PORT
    if address.count(":") == 1:
        host, port = address.split(":")
        return host, int(port)
    return address, API_PORT


def url_host(host: str) -> str:
    """``host`` as a URL's authority needs it: an IPv6 address, which ``split_address`` keeps
    bare, goes back in brackets."""
    return f"[{host}]" if ":" in host else host


def _host_key(host: str) -> str:
    """A host in one canonical form: no brackets, lower case, no trailing dot,
    and an IP address in its compressed form."""
    host = host.strip().removeprefix("[").removesuffix("]").rstrip(".").lower()
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        return host


def same_host(address: str, url: str) -> bool:
    """Whether ``url`` points at the host of a Device at ``address``. A Device's
    credentials and pin go only to its own host, never to wherever a Camera URL
    says (that would hand them to any host a student types)."""
    try:
        url_host = urlsplit(url).hostname or ""
        device_host = split_address(address)[0]
    except ValueError:
        return False
    return bool(url_host) and _host_key(url_host) == _host_key(device_host)


def _unverified_tls() -> ssl.SSLContext:
    """No CA check: Devices are self-signed. Trust comes from the pinned
    fingerprint instead, checked by _PinnedStream during every handshake."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _fingerprint_of(ssl_object: ssl.SSLObject | ssl.SSLSocket) -> str:
    return hashlib.sha256(ssl_object.getpeercert(binary_form=True)).hexdigest()


class DeviceClient:
    def __init__(self, address: str, *, username: str | None = None, password: str | None = None,
                 fingerprint: str | None = None, timeout: float = 5.0):
        self.host, self.api_port = split_address(address)
        self._auth = None
        if username is not None:
            token = base64.b64encode(f"{username}:{password or ''}".encode()).decode()
            self._auth = f"Basic {token}"
        self.fingerprint = normalize_fingerprint(fingerprint) if fingerprint else None
        self.timeout = timeout

    @classmethod
    def for_device(cls, device: Device, *, timeout: float = 5.0) -> DeviceClient:
        """A client for a registered Device, with its credentials and its pinned fingerprint."""
        return cls(device.address, username=device.username, password=device.password,
                   fingerprint=device.fingerprint, timeout=timeout)

    async def presented_fingerprint(self) -> str:
        """The SHA-256 fingerprint of the certificate the Device presents now."""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.api_port, ssl=_unverified_tls(),
                                        server_hostname=self.host),
                self.timeout)
        except (OSError, TimeoutError, ssl.SSLError) as exc:
            raise DeviceUnreachable(f"cannot reach {self.host}:{self.api_port} over TLS: {exc}") from exc
        try:
            return _fingerprint_of(writer.get_extra_info("ssl_object"))
        finally:
            writer.close()

    async def reachable(self) -> bool:
        """Whether the Device's API port accepts a TCP connection: the cheap check behind
        Offline. Nothing is sent, so it needs neither the pin nor the credentials, and a
        Device that is up but whose MediaMTX hangs still counts as reachable (its Feed goes
        Down instead)."""
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.api_port),
                                               self.timeout)
        except (OSError, TimeoutError):
            return False
        writer.close()
        return True

    async def discover(self) -> Discovery:
        """The Device's Cameras and its RTSPS port. A Camera is a path the Device
        configures by name, live or not, so one whose source is failing when the
        Device is added is still listed and shows Down until its source comes up
        (#36); a path with a live source counts too. A path that exists only
        because a reader asked for a name matching a pattern is not a Camera."""
        paths, _ = await self._request("GET", "/v3/paths/list?itemsPerPage=1000")
        config, skew = await self._request("GET", "/v3/config/global/get")
        items = paths.get("items") if isinstance(paths, dict) else None
        cameras = tuple(
            _camera_from_path(item) for item in (items if isinstance(items, list) else [])
            if isinstance(item, dict) and (item.get("available", item.get("ready"))
                                           or item.get("confName") == item.get("name"))
        )
        rtsps = config.get("rtspsAddress") if isinstance(config, dict) else None
        return Discovery(cameras=cameras, rtsps_port=_port_of(rtsps), clock_skew=skew)

    async def path_config(self, name: str) -> dict[str, Any]:
        """A path's config as the Device holds it now (``/v3/config/paths/get/{name}``)."""
        config, _ = await self._request("GET", f"/v3/config/paths/get/{_quote(name)}")
        if not isinstance(config, dict):
            raise DeviceRejected(f"the Device sent no config for path {name!r}", 200)
        return config

    async def patch_path_config(self, name: str, fields: dict[str, Any]) -> None:
        """Change some of a path's config fields; MediaMTX keeps the change in memory only."""
        await self._request("PATCH", f"/v3/config/paths/patch/{_quote(name)}", fields)

    async def started(self) -> datetime:
        """When the Device's MediaMTX started (``/v3/info``). Only a restart changes it. Go writes
        it RFC 3339 with nanoseconds, so it is parsed, never compared as text."""
        info, _ = await self._request("GET", "/v3/info")
        text = info.get("started") if isinstance(info, dict) else None
        try:
            started = datetime.fromisoformat(text) if isinstance(text, str) else None
        except ValueError:
            started = None
        if started is None or started.tzinfo is None:
            raise DeviceRejected(f"the Device's /v3/info has no usable start time: {str(text)[:40]!r}", 200)
        return started

    async def clock_skew(self) -> float | None:
        """How many seconds the Device's clock is ahead of this instance's (negative: behind),
        from the ``Date`` header of a pinned API answer; None if it sent no readable one."""
        _, skew = await self._request("GET", "/v3/info")
        return skew

    @asynccontextmanager
    async def stream(self, port: int, path: str, *,
                     read_timeout: float | None) -> AsyncIterator[AsyncIterator[bytes]]:
        """A pinned GET of ``path`` on ``port`` of this Device's own host, its body yielded as it
        arrives. The pin and the credentials are the Device API's, and so are the refusals:
        ``FingerprintMismatch`` before any credential is sent, ``DeviceUnreachable`` (also when the
        stream breaks or a read waits longer than ``read_timeout`` seconds; None waits forever),
        ``CredentialsRejected``, ``PermissionDenied`` and ``DeviceRejected``. The host is never the
        caller's to choose, so the credentials can't go anywhere else (CODING_STANDARDS rule 2)."""
        async with self._pinned("GET", port, path, read_timeout=read_timeout) as response:
            if response.status != 200:
                _refuse(response.status, await _read_bounded(response, ERROR_BODY_MAX), path,
                        self._service(port))
            yield response.aiter_stream()

    async def _request(self, method: str, path: str,
                       body: dict[str, Any] | None = None) -> tuple[Any, float | None]:
        """The JSON body of a pinned API answer (None when empty), and the clock skew its
        ``Date`` header shows."""
        async with self._pinned(method, self.api_port, path, body, read_timeout=self.timeout) as response:
            # One byte past the limit tells an answer that is too big from one that just fits.
            limit = API_BODY_MAX if response.status == 200 else ERROR_BODY_MAX
            content = await _read_bounded(response, limit + 1)
        _refuse(response.status, content, path)
        if len(content) > API_BODY_MAX:
            raise DeviceRejected(f"the Device API sent more than {API_BODY_MAX} bytes for {_endpoint(path)}",
                                 200)
        try:
            answer = json.loads(content) if content else None
        except ValueError as exc:
            raise DeviceRejected(f"the Device API sent something other than JSON for {_endpoint(path)}",
                                 200) from exc
        return answer, _clock_skew(response.headers, time.time())

    @asynccontextmanager
    async def _pinned(self, method: str, port: int, path: str, body: dict[str, Any] | None = None, *,
                      read_timeout: float | None) -> AsyncIterator[httpcore.Response]:
        """The one way to the Device with credentials: HTTPS to ``port`` on its own host, with the
        pin checked inside the handshake (``_PinnedBackend``) before the request is written. A
        transport failure while connecting or reading the body is ``DeviceUnreachable``."""
        if self.fingerprint is None:
            raise ValueError("pin a fingerprint before calling the Device API")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            raise ValueError(f"not a port: {port!r}")
        if not path.startswith("/"):  # "@other.host/..." would turn the Device's host into userinfo
            raise ValueError(f"a Device path starts with '/': {path!r}")
        timeouts = {"connect": self.timeout, "read": read_timeout, "write": self.timeout,
                    "pool": self.timeout}
        headers = {"Authorization": self._auth} if self._auth else {}
        content = None
        if body is not None:
            content = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        url = f"https://{url_host(self.host)}:{port}{path}"
        try:
            async with (
                httpcore.AsyncConnectionPool(ssl_context=_unverified_tls(),
                                             network_backend=_PinnedBackend(self.fingerprint)) as pool,
                pool.stream(method, url, headers=headers, content=content,
                            extensions={"timeout": timeouts}) as response,
            ):
                yield response
        except FingerprintMismatch:
            raise
        except (httpcore.TimeoutException, httpcore.NetworkError, httpcore.ProtocolError,
                OSError, ssl.SSLError) as exc:
            raise DeviceUnreachable(f"cannot reach {self._service(port)} at {url}: {exc!r}") from exc

    def _service(self, port: int) -> str:
        """What an error calls the service on ``port`` of the Device's host."""
        if port == self.api_port:
            return API_SERVICE
        if port == GPS_SIDECAR_PORT:
            return "the GPS Sidecar"
        return f"port {port} on the Device"


def _quote(name: str) -> str:
    # relay imports this module (same_host), so importing quote_path at the top would cycle.
    from camera_viewer.relay import quote_path
    return quote_path(name)


ERROR_TEXT_MAX = 200
ERROR_BODY_MAX = 4096  # what a refusal's body is read to, for its error text
# The most an API answer may be. A Device Kit's path list is a few kilobytes; this leaves room
# for the 1000 paths discovery asks for, and stops a Device from filling the instance's memory.
API_BODY_MAX = 4 * 1024 * 1024


def _endpoint(path: str) -> str:
    """What an error names: the query adds nothing."""
    return path.split("?")[0]


def _refuse(status: int, content: bytes, path: str, service: str = API_SERVICE) -> None:
    """Raise the typed refusal for an answer from ``service`` that is not a success. The API's
    wording is the one the UI has always shown; another port's names that port's service."""
    endpoint = _endpoint(path)
    if status == 401:
        who = "the Device" if service == API_SERVICE else service
        raise CredentialsRejected(f"{who} rejected the username or password")
    if status == 403:
        if service == API_SERVICE:
            raise PermissionDenied(f"the Device's user may not use {endpoint}: it needs the api permission")
        raise PermissionDenied(f"{service} refused the Device's user for {endpoint}")
    if status != 200:
        raise DeviceRejected(f"{service} answered {status} for {endpoint}{_error_text(content)}", status)


async def _read_bounded(response: httpcore.Response, limit: int) -> bytes:
    """At most ``limit`` bytes of a streamed body: a Device is untrusted input."""
    content = bytearray()
    async for chunk in response.aiter_stream():
        content += chunk
        if len(content) >= limit:
            break
    return bytes(content[:limit])


def _error_text(content: bytes) -> str:
    """``: <the Device's error>`` from a MediaMTX error body, cut short: it is shown to people,
    and a Device is untrusted input."""
    try:
        error = json.loads(content).get("error")
    except (ValueError, AttributeError):
        return ""
    if not isinstance(error, str) or not error.strip():
        return ""
    error = " ".join(error.split())
    return f": {error[:ERROR_TEXT_MAX]}{'…' if len(error) > ERROR_TEXT_MAX else ''}"


def _clock_skew(headers: list[tuple[bytes, bytes]], now: float) -> float | None:
    """The Device's ``Date`` header minus ``now``. A Device is untrusted input, so a header
    that is missing or unreadable means no reading, never an error."""
    for name, value in headers:
        if name.lower() == b"date":
            try:
                sent = parsedate_to_datetime(value.decode("latin-1"))
            except (TypeError, ValueError):
                return None
            if sent.tzinfo is None:  # "-0000": UTC, by RFC 5322
                sent = sent.replace(tzinfo=UTC)
            return sent.timestamp() - now
    return None


MAX_DIMENSION = 16384


def dimension(value: Any) -> int | None:
    """A width or height a Device reported, if it is a whole number of pixels in range; None
    otherwise. The one check for every dimension a Device sends (CODING_STANDARDS rule 1)."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_DIMENSION:
        return value
    return None


def _camera_from_path(item: dict[str, Any]) -> DiscoveredCamera:
    width = height = None
    tracks = item.get("tracks2")
    for track in tracks if isinstance(tracks, list) else []:
        props = track.get("codecProps") if isinstance(track, dict) else None
        if isinstance(props, dict) and dimension(props.get("width")) and dimension(props.get("height")):
            width, height = props["width"], props["height"]
            break
    return DiscoveredCamera(name=item.get("name"), width=width, height=height)


def _port_of(address: Any) -> int:
    if isinstance(address, str) and ":" in address:
        try:
            port = int(address.rsplit(":", 1)[1])
        except ValueError:
            return DEFAULT_RTSPS_PORT
        if 0 < port < 65536:
            return port
    return DEFAULT_RTSPS_PORT


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Opens TCP as usual, but refuses any TLS peer whose certificate is not the pinned one."""

    def __init__(self, fingerprint: str):
        self._inner = httpcore.AnyIOBackend()
        self._fingerprint = fingerprint

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = await self._inner.connect_tcp(host, port, timeout=timeout, local_address=local_address,
                                               socket_options=socket_options)
        return _PinnedStream(stream, self._fingerprint)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _PinnedStream(httpcore.AsyncNetworkStream):
    def __init__(self, inner: httpcore.AsyncNetworkStream, fingerprint: str):
        self._inner = inner
        self._fingerprint = fingerprint

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._inner.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._inner.write(buffer, timeout)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        tls = await self._inner.start_tls(ssl_context, server_hostname, timeout)
        presented = _fingerprint_of(tls.get_extra_info("ssl_object"))
        if presented != self._fingerprint:
            await tls.aclose()
            raise FingerprintMismatch(self._fingerprint, presented)
        return tls

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)
