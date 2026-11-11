"""Fixed Positions: a Position entered by hand for a Device that has no GPS receiver (#58).

``PUT /api/devices/<label>/fixed-position`` with ``{"latitude": ..., "longitude": ...}`` sets or
changes it and answers it back; ``DELETE`` clears it (204, also when there was none). Both answer
404 ``device_not_found`` for a Device that isn't registered. The values pass the rules a GPS fix's
coordinates pass (``positions.check_place``), so anything else is 422 ``invalid_position`` with the
reason; the body is taken as it came, so a string or a bool is refused rather than coerced.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from camera_viewer.api.errors import ApiError, registered_device
from camera_viewer.positions import InvalidFix, check_place
from camera_viewer.services import Services, services

router = APIRouter(prefix="/devices", tags=["positions"])


class FixedPositionForm(BaseModel):
    # Any, so pydantic passes each value through as sent and check_place alone judges it.
    latitude: Any = None
    longitude: Any = None


@router.put("/{label}/fixed-position")
async def set_fixed_position(label: str, form: FixedPositionForm,
                             svc: Services = Depends(services)) -> dict[str, float]:
    registered_device(svc.registry, label)
    try:
        place = check_place(form.latitude, form.longitude)
    except InvalidFix as exc:
        raise ApiError(422, "invalid_position", f"that Fixed Position can't be used: {exc}") from exc
    svc.positions.set_fixed(label, place)
    return place.public()


@router.delete("/{label}/fixed-position", status_code=204)
async def clear_fixed_position(label: str, svc: Services = Depends(services)) -> Response:
    registered_device(svc.registry, label)
    svc.positions.clear_fixed(label)
    return Response(status_code=204)
