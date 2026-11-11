"""Snapshots: a browser uploads the frame on screen; the server names and stores it.

    POST /api/snapshots?camera=<camera id>    body: the PNG (Content-Type: image/png)
                                             -> 201 {capture, camera, width, height, size}

The upload is untrusted. It must say it is ``image/png``, be no bigger than
``CV_SNAPSHOT_MAX_BYTES`` (checked against Content-Length up front and
counted while reading, so a chunked upload can't run past it), and be one
well-formed PNG (``camera_viewer.snapshots.read_png``). Only then is it
written. The file name is the Capture name for the Camera, the time the
upload arrived and the PNG's own size; nothing the client sends (a file
name, a Content-Disposition, a path) reaches it.

Refusals: 404 ``camera_not_found``, 413 ``snapshot_too_large`` (with
``max_bytes``), 415 ``unsupported_media_type``, 422 ``invalid_png``, and
507 ``low_disk`` below the disk's refuse threshold (``api/disk.py``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request

from camera_viewer.api.disk import refuse_when_low
from camera_viewer.api.errors import ApiError
from camera_viewer.captures import CaptureName
from camera_viewer.services import Services, services
from camera_viewer.snapshots import InvalidPng, read_png

router = APIRouter(prefix="/snapshots", tags=["snapshots"])


def _too_large(limit: int) -> ApiError:
    return ApiError(413, "snapshot_too_large", f"a Snapshot may be at most {limit} bytes", max_bytes=limit)


async def _read_bounded(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        raise _too_large(limit)
    body = bytearray()
    async for piece in request.stream():
        body += piece
        if len(body) > limit:
            raise _too_large(limit)
    return bytes(body)


@router.post("", status_code=201)
async def upload_snapshot(camera: str, request: Request, svc: Services = Depends(services)) -> dict[str, Any]:
    found = svc.registry.camera(camera)
    if found is None:
        raise ApiError(404, "camera_not_found", f"no Camera {camera!r}")
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != "image/png":
        raise ApiError(415, "unsupported_media_type", "a Snapshot is uploaded as image/png")
    refuse_when_low(svc)  # before reading, so a refused upload is never read
    data = await _read_bounded(request, svc.settings.snapshot_max_bytes)
    try:
        info = await asyncio.to_thread(read_png, data)  # inflating a large frame takes a moment
    except InvalidPng as exc:
        raise ApiError(422, "invalid_png", str(exc)) from exc
    when = datetime.now().astimezone()
    name = CaptureName(found.device, found.name, when, info.width, info.height, "png")
    stored = await asyncio.to_thread(svc.captures.add, data, name)
    svc.captures.changed()  # back on the event loop: every browser's Captures list
    return {"capture": stored, "camera": found.id, "width": info.width, "height": info.height,
            "size": len(data)}
