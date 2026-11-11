"""Cameras: the list, with Device, resolution and whatever other modules add;
adding a manual Camera from a raw RTSP URL; and setting a Camera's Quality
(``PUT /api/cameras/<camera id>/quality``).

A manual Camera is for a Device whose API is unreachable, or that was not
built from the Device Kit. It joins the Device named by ``device`` if there
is one (using that Device's credentials and pin), or creates a Device with
that label that has no API. To join, the URL's host must be the Device's
(422 ``host_mismatch``), or the Device's credentials would go wherever the
URL says; and a Device with a pinned certificate takes only ``rtsps://``
(422 ``rtsps_required``). An ``rtsps://`` URL is trusted on first use like
adding a Device (``api.trust``); ``rtsp://`` needs no fingerprint, and is
unencrypted. Credentials in the URL are kept as the Device's and stripped
from the stored source URL.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from camera_viewer.api.errors import ApiError, registry_error
from camera_viewer.api.trust import confirm_fingerprint, device_error
from camera_viewer.device_client import DeviceError, same_host
from camera_viewer.quality_service import QualityUnavailable, SplitsRecording
from camera_viewer.quality_store import ModeText, parse_mode
from camera_viewer.registry import Camera, Device, RegistryError, check_camera_name, check_label
from camera_viewer.services import Services, services

router = APIRouter(prefix="/cameras", tags=["cameras"])


class ManualCameraForm(BaseModel):
    device: str                       # label of the Device it belongs to, existing or new
    url: str                          # rtsp[s]://[user:pass@]host[:port]/path
    name: str | None = None           # default: the URL's path
    fingerprint: str | None = None    # the confirmed fingerprint, for a new rtsps:// Device


@router.get("")
def list_cameras(svc: Services = Depends(services)) -> list[dict[str, Any]]:
    return svc.cameras.list()


@router.post("", status_code=201)
async def add_manual_camera(form: ManualCameraForm, svc: Services = Depends(services)) -> dict[str, Any]:
    source = parse_source(form.url)
    try:
        label = check_label(form.device)
        name = check_camera_name(form.name if form.name else source.path)
    except RegistryError as exc:
        raise registry_error(exc) from exc

    device = svc.registry.get(label)
    if device is not None:
        if not same_host(device.address, source.url):
            raise ApiError(422, "host_mismatch",
                           f"the URL's host {source.host} is not Device {label!r}'s address "
                           f"{device.address}; a Camera on another host needs a Device of its own")
        if device.fingerprint and source.scheme != "rtsps":
            raise ApiError(422, "rtsps_required",
                           f"Device {label!r} has a pinned certificate, so its Cameras must be rtsps://; "
                           "a plain rtsp:// URL would send its credentials and video unencrypted")
        if any(camera.name == name for camera in device.cameras):
            raise ApiError(409, "camera_exists", f"there is already a Camera {label}/{name}")
        if source.username and (source.username, source.password) != (device.username, device.password):
            raise ApiError(422, "credentials_conflict",
                           f"the URL's credentials differ from Device {label!r}'s; a Device has one set")

    pin = ""
    if source.scheme == "rtsps":
        if device is None:
            pin = await confirm_fingerprint(source.address, form.fingerprint, what="Camera")
        elif device.fingerprint:
            pin = await confirm_fingerprint(source.address, device.fingerprint, what="Camera")
        else:
            raise ApiError(422, "device_unpinned",
                           f"Device {label!r} has no pinned certificate, so it cannot pull rtsps:// Cameras")

    camera = Camera(device=label, name=name, source_url=source.url)
    try:
        if device is None:
            svc.registry.add(Device(label=label, address=source.host, username=source.username,
                                    password=source.password, fingerprint=pin, api=False,
                                    cameras=(camera,)))
        else:
            svc.registry.add_camera(label, camera)
    except RegistryError as exc:
        raise registry_error(exc) from exc
    return svc.cameras.get(camera.id)


class QualityForm(BaseModel):
    mode: ModeText
    confirm: bool = False  # yes, split the Camera's Recording if the change drops viewers


@router.put("/{camera_id:path}/quality")
async def set_quality(camera_id: str, form: QualityForm, svc: Services = Depends(services)) -> dict[str, Any]:
    """Put a Camera on Auto or pin it to a level, through its Device's API (ADR 0009).

    A change that drops viewers while the Camera is Recording splits the Recording, so it is
    refused with 409 ``confirm_required`` unless ``confirm`` is true: the warning can't be
    skipped by a client that ignores the UI."""
    if svc.registry.camera(camera_id) is None:
        raise ApiError(404, "camera_not_found", f"no Camera {camera_id!r}")
    try:
        await svc.quality.choose(camera_id, parse_mode(form.mode), confirm=form.confirm)
    except QualityUnavailable as exc:
        raise ApiError(409, "quality_unavailable", f"{camera_id}'s Quality can't be changed: {exc}",
                       reason=exc.code) from exc
    except SplitsRecording as exc:
        raise ApiError(409, "confirm_required", f"This splits the Recording: {exc}") from exc
    except DeviceError as exc:
        raise device_error(exc) from exc
    except KeyError as exc:  # removed while the change was on its way
        raise ApiError(404, "camera_not_found", f"no Camera {camera_id!r}") from exc
    return svc.cameras.get(camera_id)


@dataclass(frozen=True)
class Source:
    scheme: str
    host: str          # as it goes in a URL: an IPv6 address in brackets
    port: int | None
    path: str          # without the leading slash
    query: str
    username: str
    password: str

    @property
    def address(self) -> str:
        """``host:port`` of the TLS endpoint, defaulting to the RTSPS port 322."""
        return f"{self.host}:{self.port or 322}"

    @property
    def url(self) -> str:
        """The source URL without credentials."""
        netloc = self.host if self.port is None else f"{self.host}:{self.port}"
        return urlunsplit((self.scheme, netloc, "/" + self.path, self.query, ""))


_HOSTNAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*")


def parse_source(url: str) -> Source:
    def invalid(why: str) -> ApiError:
        return ApiError(422, "url_invalid", f"{url!r} is not a usable RTSP URL: {why} "
                                            "(expected rtsp[s]://[user:pass@]host[:port]/path)")

    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise invalid(str(exc)) from exc
    if parts.scheme.lower() not in ("rtsp", "rtsps"):
        raise invalid("the scheme must be rtsp or rtsps")
    if parts.fragment:
        raise invalid("it has a #fragment")
    host = parts.hostname or ""
    try:
        if ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
    except ValueError:
        if not _HOSTNAME.fullmatch(host):
            raise invalid("it has no valid host") from None
    path = parts.path[1:] if parts.path.startswith("/") else parts.path
    return Source(scheme=parts.scheme.lower(), host=host, port=port, path=path, query=parts.query,
                  username=unquote(parts.username or ""), password=unquote(parts.password or ""))
