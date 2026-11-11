"""Device Registry: owns Devices and their Cameras.

The single source of truth for which Devices exist, how to reach them, and
which fingerprint each is pinned to. Other modules never mutate it; they
subscribe and react (the Relay Controller re-declares its paths, the event
bus announces the change, and a module holding something of a Device's
winds it down through ``on_removed``).

Every change is checked (labels kebab-case and unique, Camera names safe),
written to the store (``device_store.DeviceStore``, the config folder) and
only then made and announced, so what browsers see is always what survives
a restart.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from typing import Protocol


@dataclass(frozen=True)
class Camera:
    """One video source on a Device: one MediaMTX path there."""

    device: str          # the owning Device's label
    name: str            # the path name on the Device, e.g. "csi", "test-pattern"
    source_url: str      # where the Relay pulls it from, without credentials
    width: int | None = None
    height: int | None = None

    @property
    def id(self) -> str:
        """Stable across the instance: ``<device label>/<camera name>``. Also
        the Relay's path name for this Camera."""
        return f"{self.device}/{self.name}"


@dataclass(frozen=True)
class Device:
    label: str           # kebab-case, unique per instance
    address: str         # host name or IP the instance reaches the Device at
    username: str
    password: str
    fingerprint: str     # pinned SHA-256 of the Device's certificate, lowercase hex;
                         # empty only for a manual Device pulled over plain RTSP
    cameras: tuple[Camera, ...] = ()
    api: bool = True     # the instance talks to this Device's MediaMTX API; False for
                         # a Device created from a manual Camera

    def public(self) -> dict:
        """The Device as the API shows it: never the credentials."""
        return {
            "label": self.label,
            "address": self.address,
            "fingerprint": self.fingerprint,
            "api": self.api,
            "cameras": [camera.id for camera in self.cameras],
        }


class RegistryError(ValueError):
    """A change the Registry refuses. ``code`` is stable and machine-readable."""

    code = "registry_error"


class InvalidLabel(RegistryError):
    code = "label_invalid"


class DuplicateLabel(RegistryError):
    code = "label_taken"

    def __init__(self, label: str):
        super().__init__(f"a Device labelled {label!r} already exists")
        self.label = label


class DeviceNotFound(RegistryError):
    code = "device_not_found"

    def __init__(self, label: str):
        super().__init__(f"no Device labelled {label!r}")
        self.label = label


class CameraExists(RegistryError):
    code = "camera_exists"

    def __init__(self, camera_id: str):
        super().__init__(f"there is already a Camera {camera_id!r}")
        self.camera_id = camera_id


class InvalidCameraName(RegistryError):
    code = "camera_name_invalid"


LABEL_MAX = 40
CAMERA_NAME_MAX = 64
_KEBAB = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def check_label(label: object) -> str:
    """A Device label is kebab-case (lowercase letters and digits in groups
    joined by single hyphens) and at most LABEL_MAX characters, because it
    becomes the first segment of Relay path names and of Capture file names."""
    if not isinstance(label, str) or not _KEBAB.fullmatch(label) or len(label) > LABEL_MAX:
        raise InvalidLabel(
            f"the label {label!r} is not kebab-case: use lowercase letters, digits and single hyphens, "
            f"starting and ending with a letter or digit, at most {LABEL_MAX} characters (e.g. cam-2)")
    return label


_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.~-]*")


def check_camera_name(name: object) -> str:
    """A Camera name is one or more ``/``-separated segments, each starting
    with a letter or digit and holding only letters, digits and ``_ . ~ -``:
    no ``.`` or ``..`` segments, no empty segments, nothing a URL or a file
    system would read specially. Names come from Devices, and a Device is
    untrusted input."""
    if (not isinstance(name, str) or len(name) > CAMERA_NAME_MAX
            or not all(_SEGMENT.fullmatch(segment) for segment in name.split("/"))):
        raise InvalidCameraName(
            f"the Camera name {name!r} is not allowed: use letters, digits and _ . ~ - in "
            f"segments separated by /, each starting with a letter or digit, at most "
            f"{CAMERA_NAME_MAX} characters (e.g. usb-2)")
    return name


Listener = Callable[[], None]
RemovalListener = Callable[[Device], Awaitable[None]]


class Store(Protocol):
    def load(self) -> list[Device]: ...
    def save(self, devices: Iterable[Device]) -> None: ...


class DeviceRegistry:
    def __init__(self, store: Store | None = None) -> None:
        """Starts with the Devices in ``store``; without one, in memory only."""
        self._store = store
        self._devices: dict[str, Device] = {d.label: d for d in store.load()} if store else {}
        self._listeners: list[Listener] = []
        self._removal_listeners: list[RemovalListener] = []

    def subscribe(self, listener: Listener) -> None:
        """Call ``listener()`` after every change."""
        self._listeners.append(listener)

    def on_removed(self, listener: RemovalListener) -> None:
        """Before a Device is removed, await ``listener(device)``, with the
        Device still in the Registry (so the Relay still has its paths): a
        module that holds something of the Device's (the Recorder's
        Recordings) winds it down there. Each listener bounds its own wait."""
        self._removal_listeners.append(listener)

    def add(self, device: Device) -> None:
        """Add a new Device. Raises InvalidLabel, DuplicateLabel, InvalidCameraName."""
        if device.label in self._devices:
            raise DuplicateLabel(device.label)
        self._put(device)

    def add_camera(self, label: str, camera: Camera) -> Device:
        """Add a manual Camera to an existing Device; it uses the Device's
        credentials and pin. Raises DeviceNotFound, CameraExists, InvalidCameraName."""
        device = self._require(label)
        if any(existing.name == camera.name for existing in device.cameras):
            raise CameraExists(camera.id)
        updated = replace(device, cameras=(*device.cameras, camera))
        self._put(updated)
        return updated

    async def remove(self, label: str) -> Device:
        """Forget a Device and its Cameras, once every removal listener has
        wound down what it held of them. Raises DeviceNotFound."""
        device = self._require(label)
        for listener in self._removal_listeners:
            await listener(device)
        device = self._require(label)  # a second removal may have won meanwhile
        devices = dict(self._devices)
        del devices[label]
        self._commit(devices)
        return device

    def repin(self, label: str, fingerprint: str) -> Device:
        """Pin a Device to a new certificate. Only ever called on a student's
        explicit confirmation (the re-pin endpoint); nothing re-pins on its own.
        Raises DeviceNotFound."""
        device = replace(self._require(label), fingerprint=fingerprint)
        self._put(device)
        return device

    def get(self, label: str) -> Device | None:
        return self._devices.get(label)

    def devices(self) -> list[Device]:
        return list(self._devices.values())

    def cameras(self) -> list[Camera]:
        return [camera for device in self._devices.values() for camera in device.cameras]

    def camera(self, camera_id: str) -> Camera | None:
        """The Camera with this id (``<device label>/<camera name>``), if there is one."""
        return next((camera for camera in self.cameras() if camera.id == camera_id), None)

    # -- internals ------------------------------------------------------------

    def _require(self, label: str) -> Device:
        device = self._devices.get(label)
        if device is None:
            raise DeviceNotFound(label)
        return device

    def _put(self, device: Device) -> None:
        """Add or replace one Device, after checking it. Every change but a
        removal comes through here."""
        check_label(device.label)
        for camera in device.cameras:
            check_camera_name(camera.name)
            if camera.device != device.label:
                raise ValueError(f"Camera {camera.id!r} does not belong to Device {device.label!r}")
        if len({camera.name for camera in device.cameras}) != len(device.cameras):
            raise ValueError(f"Device {device.label!r} has two Cameras with the same name")
        self._commit({**self._devices, device.label: device})

    def _commit(self, devices: dict[str, Device]) -> None:
        """Persist, then make the change and announce it. A store that fails
        raises before anything changed."""
        if self._store is not None:
            self._store.save(devices.values())
        self._devices = devices
        for listener in self._listeners:
            listener()
