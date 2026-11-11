"""Events: the one server-sent-events stream of shared state."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from camera_viewer.services import Services, services

router = APIRouter(tags=["events"])


@router.get("/events")
def event_stream(svc: Services = Depends(services)) -> StreamingResponse:
    return StreamingResponse(
        svc.events.stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
