"""Captures: the Recordings and Snapshots in the captures folder.

    GET    /api/captures                   every Capture, newest first:
                                           [{name, kind, size, modified, interrupted, in_progress}, ...]
    GET    /api/captures/<name>            the file, inline (preview; Range requests work)
    GET    /api/captures/<name>?download=1 the same bytes as an attachment named <name>
    DELETE /api/captures/<name>            -> 204; the file leaves the captures folder

The list is also the ``captures`` event, pushed to every browser whenever a
Capture is added or deleted. ``kind`` is ``recording`` or ``snapshot``;
``interrupted`` marks a file of a Recording cut off by a restart, and
``in_progress`` one whose Recording has not finished yet.

``<name>`` comes from the client. Only a name ``CaptureStore.find`` accepts
reaches the disk: a Capture-shaped name of a regular file directly in the
folder, never a path, dot-entry or symlink (404 ``capture_not_found``).
Deleting a file of an unfinished Recording is refused (409
``capture_recording``): stop the Recording first.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Response
from fastapi.responses import FileResponse

from camera_viewer.api.errors import ApiError
from camera_viewer.captures import Capture, CaptureNotFound, CaptureStore
from camera_viewer.recorder import Recorder
from camera_viewer.services import Services, services

router = APIRouter(prefix="/captures", tags=["captures"])

MEDIA_TYPES = {"recording": "video/mp4", "snapshot": "image/png"}


def capture_list(store: CaptureStore, recorder: Recorder) -> list[dict[str, Any]]:
    """The Captures list as browsers see it."""
    interrupted = {name for r in recorder.interrupted() for name in r.captures}
    in_progress = {name for r in recorder.unfinished() for name in r.captures}
    return [{"name": c.name, "kind": c.kind, "size": c.size,
             "modified": datetime.fromtimestamp(c.modified).astimezone().isoformat(),
             "interrupted": c.name in interrupted, "in_progress": c.name in in_progress}
            for c in store.captures()]


@router.get("")
async def list_captures(svc: Services = Depends(services)) -> list[dict[str, Any]]:
    return capture_list(svc.captures, svc.recorder)


def _find(store: CaptureStore, name: str) -> Capture:
    try:
        return store.find(name)
    except CaptureNotFound:
        raise ApiError(404, "capture_not_found", f"no Capture {name!r}") from None


@router.get("/{name}")
async def get_capture(name: str, download: bool = False, svc: Services = Depends(services)) -> FileResponse:
    capture = _find(svc.captures, name)
    return FileResponse(svc.captures.path(capture), media_type=MEDIA_TYPES[capture.kind],
                        filename=capture.name, content_disposition_type="attachment" if download else "inline",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})


@router.delete("/{name}", status_code=204)
async def delete_capture(name: str, svc: Services = Depends(services)) -> Response:
    # Checked and deleted with no await in between, so no Recording can claim
    # the file meanwhile (the Recorder runs on this same event loop).
    recording = next((r for r in svc.recorder.unfinished() if name in r.captures), None)
    if recording is not None:
        raise ApiError(409, "capture_recording",
                       f"{name} belongs to a Recording still running on {recording.camera.id}; stop it first",
                       recording=recording.id)
    try:
        svc.captures.delete(name)
    except CaptureNotFound:
        raise ApiError(404, "capture_not_found", f"no Capture {name!r}") from None
    return Response(status_code=204)
