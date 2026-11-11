"""Devices: list, add with trust on first use, and remove.

Adding is two requests from the browser (see ``api.trust``). Only once the
fingerprint is confirmed is the Device's API called with credentials, pinned
to it, to discover its Cameras.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from camera_viewer.api.errors import ApiError, registry_error
from camera_viewer.api.trust import confirm_fingerprint, device_error
from camera_viewer.device_client import DeviceClient, DeviceError, split_address, url_host
from camera_viewer.registry import Camera, Device, RegistryError, check_camera_name, check_label
from camera_viewer.relay import quote_path
from camera_viewer.services import Services, services

router = APIRouter(prefix="/devices", tags=["devices"])


class DeviceForm(BaseModel):
    address: str
    label: str
    username: str
    password: str
    fingerprint: str | None = None


@router.get("")
def list_devices(svc: Services = Depends(services)) -> list[dict[str, Any]]:
    return [svc.device_view(device) for device in svc.registry.devices()]


@router.delete("/{label}", status_code=204)
async def remove_device(label: str, svc: Services = Depends(services)) -> Response:
    """Remove a decommissioned Device: its Cameras leave every browser and the
    Relay stops pulling them."""
    try:
        await svc.registry.remove(label)
    except RegistryError as exc:
        raise registry_error(exc) from exc
    return Response(status_code=204)


@router.post("", status_code=201)
async def add_device(form: DeviceForm, svc: Services = Depends(services)) -> dict[str, Any]:
    # Check the label before touching the network: a bad label is the student's
    # to fix, whatever the Device says.
    try:
        check_label(form.label)
    except RegistryError as exc:
        raise registry_error(exc) from exc
    if svc.registry.get(form.label) is not None:
        raise ApiError(409, "label_taken", f"a Device labelled {form.label!r} already exists")

    confirmed = await confirm_fingerprint(form.address, form.fingerprint)
    client = DeviceClient(form.address, username=form.username, password=form.password,
                          fingerprint=confirmed)
    try:
        discovery = await client.discover()
    except DeviceError as exc:
        raise device_error(exc) from exc

    # A Device is untrusted input: a Camera whose name is unsafe, or repeated,
    # is left out and reported, never added.
    cameras, skipped = [], []
    for found in discovery.cameras:
        try:
            name = check_camera_name(found.name)
        except RegistryError as exc:
            skipped.append({"name": str(found.name), "reason": str(exc)})
            continue
        if any(camera.name == name for camera in cameras):
            skipped.append({"name": name, "reason": f"the Device reported {name!r} more than once"})
            continue
        cameras.append(Camera(device=form.label, name=name,
                              source_url=rtsps_url(form.address, discovery.rtsps_port, name),
                              width=found.width, height=found.height))
    device = Device(label=form.label, address=form.address, username=form.username,
                    password=form.password, fingerprint=confirmed, cameras=tuple(cameras))
    try:
        svc.registry.add(device)
    except RegistryError as exc:
        raise registry_error(exc) from exc
    svc.certificates.observe(device.label, confirmed)  # what it presented a moment ago
    svc.clocks.observe(device.label, discovery.clock_skew)  # so a wrong clock is reported right away
    await svc.quality.sync(device.label, read_configs=True)  # each Camera's level shows at once, not at the next check
    return {**svc.device_view(device), "skipped_cameras": skipped}


def rtsps_url(address: str, port: int, camera: str) -> str:
    host, _ = split_address(address)
    return f"rtsps://{url_host(host)}:{port}/{quote_path(camera)}"
