"""Feeds: a WHEP endpoint per Camera, proxied to the Relay by the Playback Gateway.

    POST   /api/feeds/<camera id>/whep            SDP offer in, 201 + SDP answer out
    DELETE /api/feeds/<camera id>/whep/<session>  end the session

A Camera id contains a slash (``<device>/<camera>``), hence the path converter.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response

from camera_viewer.api.errors import ApiError
from camera_viewer.playback import PlaybackError
from camera_viewer.services import Services, services

router = APIRouter(prefix="/feeds", tags=["feeds"])


def _require_camera(svc: Services, camera_id: str) -> None:
    if svc.registry.camera(camera_id) is None:
        raise ApiError(404, "camera_not_found", f"no Camera {camera_id!r}")


@router.post("/{camera_id:path}/whep", status_code=201)
async def whep_offer(camera_id: str, request: Request, svc: Services = Depends(services)) -> Response:
    _require_camera(svc, camera_id)
    try:
        answer = await svc.playback.offer(camera_id, await request.body())
    except PlaybackError as exc:
        status = 503 if exc.status in (404, None) else 502
        raise ApiError(status, "feed_unavailable", str(exc)) from exc
    location = f"/api/feeds/{quote(camera_id)}/whep/{quote(answer.session, safe='')}"
    return Response(content=answer.sdp, status_code=201, media_type="application/sdp",
                    headers={**answer.headers, "Location": location})


@router.delete("/{camera_id:path}/whep/{session}", status_code=204)
async def whep_close(camera_id: str, session: str, svc: Services = Depends(services)) -> Response:
    _require_camera(svc, camera_id)
    await svc.playback.close(camera_id, session)
    return Response(status_code=204)
