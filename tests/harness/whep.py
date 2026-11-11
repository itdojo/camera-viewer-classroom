"""Open a Feed over WHEP without a browser, to prove at Seam A that it plays.

The Relay answers a WHEP offer with 201 and an SDP answer only once the
Camera's source is ready, which for an on-demand path means the Relay has
connected to the Device and is receiving the Feed. So a 201 whose answer
carries an H.264 video section is the Seam A form of "this Feed plays". The
offer is a recvonly H.264 offer captured from Chromium; ICE never completes,
so no media flows to the test, and the session is deleted afterwards.
"""

from __future__ import annotations

from contextlib import contextmanager
from urllib.parse import quote

import httpx

OFFER = "\r\n".join([
    "v=0",
    "o=- 3040085447378779709 2 IN IP4 127.0.0.1",
    "s=-",
    "t=0 0",
    "a=group:BUNDLE 0",
    "a=msid-semantic: WMS",
    "m=video 9 UDP/TLS/RTP/SAVPF 102 108",
    "c=IN IP4 0.0.0.0",
    "a=rtcp:9 IN IP4 0.0.0.0",
    "a=ice-ufrag:rspO",
    "a=ice-pwd:NPZOq008EayV3HEJNTN2XLau",
    "a=ice-options:trickle",
    "a=fingerprint:sha-256 9B:39:FC:6D:BC:C3:0B:5E:55:AB:32:B2:71:15:DC:EA:"
    "A1:C0:41:A2:0C:00:BA:E2:6A:11:6C:FC:A7:5D:FC:7A",
    "a=setup:actpass",
    "a=mid:0",
    "a=recvonly",
    "a=rtcp-mux",
    "a=rtcp-rsize",
    "a=rtpmap:102 H264/90000",
    "a=rtcp-fb:102 nack",
    "a=rtcp-fb:102 nack pli",
    "a=fmtp:102 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f",
    "a=rtpmap:108 H264/90000",
    "a=rtcp-fb:108 nack",
    "a=rtcp-fb:108 nack pli",
    "a=fmtp:108 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
    "",
])


def feed_url(camera_id: str) -> str:
    return "/api/feeds/" + "/".join(quote(part, safe="") for part in camera_id.split("/")) + "/whep"


def offer(api: httpx.Client, camera_id: str) -> httpx.Response:
    """POST the canned offer to a Camera's WHEP endpoint. Close the session
    with ``api.delete(response.headers["location"])`` when done."""
    return api.post(feed_url(camera_id), content=OFFER.encode(),
                    headers={"Content-Type": "application/sdp"}, timeout=30)


def plays(response: httpx.Response) -> bool:
    return response.status_code == 201 and "H264/90000" in response.text


@contextmanager
def watching(api: httpx.Client, camera_id: str):
    """Hold a Feed open for the block; yields the WHEP response."""
    response = offer(api, camera_id)
    try:
        yield response
    finally:
        if response.status_code == 201:
            api.delete(response.headers["location"])
