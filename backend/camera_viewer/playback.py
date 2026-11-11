"""Playback Gateway: the WHEP signalling proxy from browser to Relay (ADR 0001).

The Relay's WHEP endpoint listens on loopback only, so the only way a
browser can open a Feed is through here, behind whatever the API requires
(a session, once Auth lands). Only signalling passes through: the SDP offer
in, the SDP answer out, and the session's teardown. Media then flows from
the Relay's WebRTC UDP port straight to the browser.

The Relay's session URL is rewritten to an opaque id the API serves under
the Camera's Feed, so the browser never learns a Relay address.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from camera_viewer.relay import RelayController, RelayCredentials, RelayUnavailable, quote_path


class PlaybackError(Exception):
    """The Relay would not play the Feed. ``status`` is the Relay's HTTP status, if it answered."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class WhepAnswer:
    sdp: bytes
    session: str                                   # opaque id for the teardown
    headers: dict[str, str] = field(default_factory=dict)  # WHEP headers worth passing on


RELAY_RECOVERY_SECONDS = 10.0

# Response headers a WHEP client may use (ICE servers, trickle ETag).
PASS_THROUGH = ("etag", "link", "accept-patch")


class PlaybackGateway:
    def __init__(self, relay: RelayController, whep_url: str, *, client: httpx.AsyncClient | None = None,
                 credentials: RelayCredentials = RelayCredentials()):
        self._relay = relay
        # The first read of an on-demand path waits for the Device to start
        # sending (sourceOnDemandStartTimeout, 10 s by default). The Relay
        # lets only its internal user read; the browser never learns it.
        self._client = client or httpx.AsyncClient(base_url=whep_url, timeout=20,
                                                   auth=credentials.httpx_auth())
        self._opening: dict[str, int] = {}

    def opening(self, camera_id: str) -> bool:
        """Whether a browser is opening this Camera's Feed right now. The Feed Monitor counts
        it as demand, since the Relay lists a reader only once the source is ready."""
        return self._opening.get(camera_id, 0) > 0

    async def offer(self, camera_id: str, sdp: bytes) -> WhepAnswer:
        self._opening[camera_id] = self._opening.get(camera_id, 0) + 1
        try:
            return await self._offer(camera_id, sdp)
        finally:
            self._opening[camera_id] -= 1
            if not self._opening[camera_id]:
                del self._opening[camera_id]

    async def _offer(self, camera_id: str, sdp: bytes) -> WhepAnswer:
        # A Relay that just crashed is back within a second or two (the
        # supervisor restarts it); wait for it rather than fail the viewer.
        deadline = asyncio.get_running_loop().time() + RELAY_RECOVERY_SECONDS
        # A change to the path that hasn't reached the Relay yet (a re-pin
        # just now) would have this read pull with the old config and fail.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._relay.settled(camera_id), RELAY_RECOVERY_SECONDS)
        while True:
            try:
                await self._relay.ensure(camera_id)
                response = await self._client.post(f"/{quote_path(camera_id)}/whep", content=sdp,
                                                    headers={"Content-Type": "application/sdp"})
                break
            except (RelayUnavailable, httpx.TransportError) as exc:
                if asyncio.get_running_loop().time() >= deadline:
                    raise PlaybackError(f"the Relay is not reachable: {exc!r}") from exc
                await asyncio.sleep(0.5)
        if response.status_code != 201:
            raise PlaybackError(response.text.strip() or f"Relay answered {response.status_code}",
                                response.status_code)
        location = response.headers.get("location", "")
        session = location.rstrip("/").rsplit("/", 1)[-1]
        headers = {name: response.headers[name] for name in PASS_THROUGH if name in response.headers}
        return WhepAnswer(sdp=response.content, session=session, headers=headers)

    async def close(self, camera_id: str, session: str) -> None:
        try:
            await self._client.delete(f"/{quote_path(camera_id)}/whep/{quote(session, safe='')}")
        except httpx.HTTPError:
            pass  # the Relay drops idle WebRTC sessions on its own

    async def aclose(self) -> None:
        await self._client.aclose()

