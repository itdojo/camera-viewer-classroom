"""Trails: a Device's accepted fixes of the last ``POSITION_TRAIL_SECONDS``, from memory (#60).

``GET /api/devices/<label>/trail`` answers ``{"seconds": <trail length>, "fixes": [...]}``, oldest
first, each fix ``{latitude, longitude, gps_time, age}`` with ``age`` in seconds since it arrived
(``PositionService.trail``). A Device with no fixes yet, or only a Fixed Position, has an empty
Trail; one that isn't registered is 404 ``device_not_found``. The map reads it once when it opens
and extends it from ``positions``, so the Trail itself never travels over SSE.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from camera_viewer.api.errors import registered_device
from camera_viewer.services import Services, services

router = APIRouter(prefix="/devices", tags=["positions"])


@router.get("/{label}/trail")
async def get_trail(label: str, svc: Services = Depends(services)) -> dict[str, Any]:
    registered_device(svc.registry, label)
    return svc.positions.trail(label)
