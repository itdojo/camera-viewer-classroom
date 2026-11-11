"""Clock Watch: notices when a Device's clock is too far off the instance's for RTSPS.

The Relay pulls over RTSPS, which keys its encryption with a timestamped
MIKEY message, and the Device's MediaMTX refuses one more than an hour off
its own clock ("NTP difference is too high"). A Device without NTP (a Pi on
an isolated classroom network) therefore adds fine, then never delivers a
frame, and the Relay sees only a bare 400, which other faults produce too.

So this watch reads each Device's clock from the ``Date`` header of a pinned
API answer: once when the Device is added (``observe``, from discovery) and
every few seconds after. A skew past the tolerance is its own condition,
which the Feed Monitor maps to Feed Status Down with the reason "Device
clock wrong". Like the Certificate Watch, it only ever reports; an
unreachable Device keeps its last reading.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from camera_viewer.device_client import DeviceClient, DeviceError
from camera_viewer.registry import Device, DeviceRegistry

log = logging.getLogger("camera_viewer.clocks")

Listener = Callable[[], None]

REASON = "Device clock wrong"
TOLERANCE_SECONDS = 3600.0  # MediaMTX's (gortsplib's) MIKEY timestamp check


class ClockWatch:
    def __init__(self, registry: DeviceRegistry, *, interval: float = 10.0, timeout: float = 3.0) -> None:
        self._registry = registry
        self._interval = interval
        self._timeout = timeout
        self._skew: dict[str, float] = {}  # label -> seconds the Device's clock is ahead (negative: behind)
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> None:
        """Call ``listener()`` whenever a Device's clock state changes."""
        self._listeners.append(listener)

    def skew(self, label: str) -> float | None:
        """The Device's last read clock skew in seconds, or None if it has not been read."""
        return self._skew.get(label)

    def wrong(self, label: str) -> bool:
        """True while the Device's clock is too far off for RTSPS to work."""
        skew = self._skew.get(label)
        return skew is not None and abs(skew) > TOLERANCE_SECONDS

    def view(self, label: str) -> dict:
        """The fields this watch adds to a Device's API view."""
        skew = self._skew.get(label)
        return {"clock_wrong": self.wrong(label), "clock_skew_seconds": None if skew is None else round(skew)}

    def observe(self, label: str, skew: float | None) -> None:
        """Record a Device's clock skew now (from the check loop, or from discovery)."""
        if skew is None:
            return
        before = self._state(label)
        self._skew[label] = skew
        if self._state(label) != before:
            if self.wrong(label):
                log.warning("Device %s's clock is %+.0f s off the instance's", label, skew)
            for listener in self._listeners:
                listener()

    async def check(self, device: Device) -> None:
        client = DeviceClient.for_device(device, timeout=self._timeout)
        try:
            skew = await client.clock_skew()
        except DeviceError:  # unreachable, or a changed certificate: other modules report those
            return
        if self._registry.get(device.label) is not None:  # still registered after the await
            self.observe(device.label, skew)

    async def check_all(self) -> None:
        # A Device made from a manual Camera has no Device API to read a clock from.
        devices = [device for device in self._registry.devices() if device.api]
        for label in self._skew.keys() - {device.label for device in devices}:
            del self._skew[label]
        await asyncio.gather(*(self.check(device) for device in devices))

    async def run(self) -> None:
        while True:
            try:
                await self.check_all()
            except Exception:  # never let one bad pass end the watch
                log.exception("clock check failed")
            await asyncio.sleep(self._interval)

    def _state(self, label: str) -> tuple[bool, int | None]:
        # Listeners hear of a change in whether the clock is wrong, or of a skew that moved by
        # a minute, never of the second-to-second jitter of a whole-second Date header.
        skew = self._skew.get(label)
        return self.wrong(label), None if skew is None else round(skew / 60)
