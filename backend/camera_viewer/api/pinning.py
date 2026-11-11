"""Re-pinning: trust a rebuilt Device's new certificate, on the student's say-so.

``PUT /api/devices/<label>/fingerprint`` with ``{"fingerprint": ...}``. It is
the confirmation step of adding a Device (``api.trust``) applied to a Device
that already exists: the instance reads the certificate the Device presents
now and re-pins only if the student sent exactly that fingerprint. Without
one it answers 409 ``fingerprint_unconfirmed`` carrying the presented
fingerprint, so the browser can show it. This endpoint is the only caller of
``DeviceRegistry.repin``: nothing re-pins without an explicit user action.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from camera_viewer.api.errors import ApiError, registered_device, registry_error
from camera_viewer.api.trust import confirm_fingerprint
from camera_viewer.registry import RegistryError
from camera_viewer.services import Services, services

router = APIRouter(prefix="/devices", tags=["devices"])


class PinForm(BaseModel):
    fingerprint: str | None = None


@router.put("/{label}/fingerprint")
async def repin_device(label: str, form: PinForm, svc: Services = Depends(services)) -> dict[str, Any]:
    device = registered_device(svc.registry, label)
    if not device.api:
        raise ApiError(422, "repin_unsupported",
                       "this Device was made from a manual Camera and has no Device API; "
                       "remove the Device and add its Camera again to trust a new certificate")
    confirmed = await confirm_fingerprint(device.address, form.fingerprint)
    try:
        repinned = svc.registry.repin(label, confirmed)  # raises if removed meanwhile
    except RegistryError as exc:
        raise registry_error(exc) from exc
    svc.certificates.observe(label, confirmed)
    return svc.device_view(repinned)
