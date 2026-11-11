"""Position Demand: a browser's say-so that it wants a Device's Position at full rate (ADR 0012).

``PUT /api/devices/<label>/position-demand`` renews it and answers 204. The browser sends one
every 10 seconds while a Camera of that Device is in Single View (``js/position-demand.js``);
the Position Service runs the Device's stream at one fix a second until the linger after the
last renewal runs out. There is nothing to release: a closed or crashed tab simply stops
renewing. Behind the session like every route.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from camera_viewer.api.errors import registry_error
from camera_viewer.registry import DeviceNotFound
from camera_viewer.services import Services, services

router = APIRouter(prefix="/devices", tags=["positions"])


@router.put("/{label}/position-demand", status_code=204)
# async: a renewal may reopen the stream, which starts a task on the event loop
async def renew_position_demand(label: str, svc: Services = Depends(services)) -> Response:
    if not svc.positions.renew(label):
        raise registry_error(DeviceNotFound(label))
    return Response(status_code=204)
