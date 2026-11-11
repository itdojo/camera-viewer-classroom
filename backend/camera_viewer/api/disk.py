"""Disk: free and total space on the filesystem holding the captures folder.

    GET /api/disk    -> {free, total, level, refusing, checked, thresholds}

``level`` is ``ok``, ``warn``, ``refuse`` or ``finalize``, the most severe
threshold crossed (see ``camera_viewer.disk``). The same object is pushed to
every browser as the ``disk`` event after every measurement.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from camera_viewer.api.errors import ApiError
from camera_viewer.services import Services, services

router = APIRouter(prefix="/disk", tags=["disk"])


@router.get("")
async def disk_space(svc: Services = Depends(services)) -> dict[str, Any]:
    return svc.disk.check().public()


def refuse_when_low(svc: Services) -> None:
    """Raise 507 ``low_disk`` if free space is below the refuse threshold.
    Called before anything is written for a new Recording or Snapshot."""
    space = svc.disk.check()
    if space.refusing:
        raise ApiError(507, "low_disk", space.refusal_message(), free=space.free,
                       refuse_bytes=space.thresholds.refuse_bytes)
