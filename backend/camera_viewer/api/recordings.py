"""Recordings: start, stop, and list the unfinished ones.

    GET  /api/recordings                 every Recording not yet finalized
    GET  /api/recordings/presets         {"presets": [<seconds>, ...]}, the timed durations offered
    GET  /api/recordings/<id>            one Recording, finalized or not (Interrupted ones survive restarts)
    POST /api/recordings                 {"camera": <camera id>, "duration": <preset seconds>?}
                                         -> 201, the new Recording (no duration: until stopped)
                                         -> 507 low_disk below the refuse threshold (api/disk.py)
    POST /api/recordings/<id>/stop       -> 200, the finalized Recording with its Captures

Stopping waits until the Relay has finalized the last file and the Capture
Store has named it, so the answer lists the Recording's Captures.

The Relay's segment hooks reach the Recorder through ``hook_router``,
mounted outside ``/api``: loopback only, and authenticated by a per-process
token the Recorder writes into the hook command, not by a session.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from camera_viewer.api.disk import refuse_when_low
from camera_viewer.api.errors import ApiError
from camera_viewer.hook import TOKEN_HEADER
from camera_viewer.recorder import AlreadyRecording
from camera_viewer.services import Services, services

router = APIRouter(prefix="/recordings", tags=["recordings"])
hook_router = APIRouter(prefix="/internal/recorder", include_in_schema=False)


class StartForm(BaseModel):
    camera: str
    duration: int | None = None  # one of the presets, in seconds; None records until stopped


@router.get("")
async def list_recordings(svc: Services = Depends(services)) -> list[dict[str, Any]]:
    return [recording.public() for recording in svc.recorder.unfinished()]


@router.get("/presets")
async def list_presets(svc: Services = Depends(services)) -> dict[str, list[int]]:
    return {"presets": list(svc.settings.recording_presets)}


@router.post("", status_code=201)
async def start_recording(form: StartForm, svc: Services = Depends(services)) -> dict[str, Any]:
    camera = svc.registry.camera(form.camera)
    if camera is None:
        raise ApiError(404, "camera_not_found", f"no Camera {form.camera!r}")
    presets = svc.settings.recording_presets
    if form.duration is not None and form.duration not in presets:
        raise ApiError(422, "unknown_preset", f"{form.duration} s is not a preset duration",
                       presets=list(presets))
    refuse_when_low(svc)
    try:
        recording = svc.recorder.start(camera, form.duration)
    except AlreadyRecording as exc:
        raise ApiError(409, "already_recording", str(exc), recording=exc.recording.public()) from exc
    return recording.public()


@router.get("/{recording_id}")
async def get_recording(recording_id: str, svc: Services = Depends(services)) -> dict[str, Any]:
    if (recording := svc.recorder.get(recording_id)) is None:
        raise ApiError(404, "recording_not_found", f"no Recording {recording_id!r}")
    return recording.public()


@router.post("/{recording_id}/stop")
async def stop_recording(recording_id: str, svc: Services = Depends(services)) -> dict[str, Any]:
    if svc.recorder.get(recording_id) is None:
        raise ApiError(404, "recording_not_found", f"no Recording {recording_id!r}")
    return (await svc.recorder.stop(recording_id)).public()


class SegmentEvent(BaseModel):
    event: str     # "create" or "complete"
    path: str      # the Relay path, which is the Camera id
    segment: str   # the segment file, inside the captures folder's staging area


@hook_router.post("/segments", status_code=204)
async def segment_hook(event: SegmentEvent, request: Request, svc: Services = Depends(services)) -> None:
    token = request.headers.get(TOKEN_HEADER, "")
    if not secrets.compare_digest(token, svc.hook_token):
        raise ApiError(403, "forbidden", "not the Relay")
    segment = Path(event.segment)
    if event.event == "create":
        await svc.recorder.segment_created(event.path, segment)
    elif event.event == "complete":
        await svc.recorder.segment_completed(event.path, segment)
    else:
        raise ApiError(422, "unknown_event", f"unknown segment event {event.event!r}")
