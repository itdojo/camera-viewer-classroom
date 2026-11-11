"""Certificate Watch: notices when a Device presents a certificate other than its pin.

A rebuilt Device comes back with a new certificate. The Relay then refuses to
pull it (``sourceFingerprint``) and the Device Client refuses its API, which
is right, but only shows up once someone watches. This watch reads the
certificate every registered Device presents, every few seconds, without
trusting it and without sending credentials, and reports a mismatch as its
own condition: "certificate changed".

It only ever *reports*. Nothing here changes a pin: re-pinning is the Device
Registry's ``repin``, called only from the re-pin endpoint when a student
confirms the new fingerprint.

The Feed Monitor maps ``certificate_changed`` to Feed Status Down with the
reason "certificate changed".
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from camera_viewer.device_client import DeviceClient, DeviceError
from camera_viewer.registry import Camera, Device, DeviceRegistry

log = logging.getLogger("camera_viewer.certificates")

Listener = Callable[[], None]

REASON = "certificate changed"


class CertificateWatch:
    def __init__(self, registry: DeviceRegistry, *, interval: float = 5.0, timeout: float = 3.0) -> None:
        self._registry = registry
        self._interval = interval
        self._timeout = timeout
        self._presented: dict[str, str] = {}  # label -> last fingerprint the Device presented
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> None:
        """Call ``listener()`` whenever a Device's certificate state changes."""
        self._listeners.append(listener)

    def presented(self, label: str) -> str | None:
        """The fingerprint the Device last presented, or None if it has not been read yet."""
        return self._presented.get(label)

    def changed(self, label: str) -> bool:
        """True while the Device presents a certificate other than the one it is pinned to."""
        device = self._registry.get(label)
        presented = self._presented.get(label)
        return device is not None and presented is not None and presented != device.fingerprint

    def view(self, label: str) -> dict:
        """The fields this watch adds to a Device's and a Camera's API view."""
        return {"certificate_changed": self.changed(label), "presented_fingerprint": self.presented(label)}

    def camera_fields(self, camera: Camera) -> dict:
        """Camera Catalog field provider: a Camera inherits its Device's certificate state."""
        return {"certificate_changed": self.changed(camera.device)}

    def observe(self, label: str, fingerprint: str) -> None:
        """Record what a Device presents now (from the check loop, or from anyone else who just read it)."""
        before = self.changed(label), self._presented.get(label)
        self._presented[label] = fingerprint
        if (self.changed(label), fingerprint) != before:
            if self.changed(label):
                log.warning("Device %s presents certificate %s, not its pin", label, fingerprint)
            for listener in self._listeners:
                listener()

    async def check(self, device: Device) -> None:
        """Read one Device's certificate now. An unreachable Device keeps its last known state:
        whether it is reachable at all is the Feed Monitor's business, not this watch's."""
        try:
            presented = await DeviceClient(device.address, timeout=self._timeout).presented_fingerprint()
        except DeviceError:
            return
        if self._registry.get(device.label) is not None:  # still registered after the await
            self.observe(device.label, presented)

    async def check_all(self) -> None:
        # A Device made from a manual Camera has no Device API to read a certificate from.
        devices = [device for device in self._registry.devices() if device.api]
        for label in self._presented.keys() - {device.label for device in devices}:
            del self._presented[label]
        await asyncio.gather(*(self.check(device) for device in devices))

    async def run(self) -> None:
        while True:
            try:
                await self.check_all()
            except Exception:  # never let one bad pass end the watch
                log.exception("certificate check failed")
            await asyncio.sleep(self._interval)
