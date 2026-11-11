"""The instance's long-lived modules, built once per app and shared by every router.

Routers take them with ``svc: Services = Depends(services)``. A ticket that
adds a module (Feed Monitor, Recorder, Capture Store, ...) adds one field
here and builds and wires it in ``camera_viewer.app.build_services``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import Request

from camera_viewer.auth import Auth
from camera_viewer.cameras import CameraCatalog
from camera_viewer.captures import CaptureStore
from camera_viewer.certificates import CertificateWatch
from camera_viewer.clocks import ClockWatch
from camera_viewer.config import Settings
from camera_viewer.disk import DiskMonitor
from camera_viewer.events import EventBus
from camera_viewer.feed_monitor import FeedMonitor
from camera_viewer.playback import PlaybackGateway
from camera_viewer.positions import PositionService
from camera_viewer.quality_service import QualityService
from camera_viewer.recorder import Recorder
from camera_viewer.registry import Device, DeviceRegistry
from camera_viewer.relay import RelayController


@dataclass
class Services:
    settings: Settings
    auth: Auth
    events: EventBus
    registry: DeviceRegistry
    cameras: CameraCatalog
    relay: RelayController
    playback: PlaybackGateway
    certificates: CertificateWatch
    clocks: ClockWatch
    captures: CaptureStore
    recorder: Recorder
    hook_token: str  # what the Relay's segment hooks must present (see api/recordings.py)
    feeds: FeedMonitor
    disk: DiskMonitor
    quality: QualityService
    positions: PositionService
    map_extract: Path | None  # the Map Extract chosen at startup (camera_viewer.maps)

    def device_view(self, device: Device) -> dict:
        return device_view(device, self.certificates, self.clocks, self.positions)


def device_view(device: Device, certificates: CertificateWatch, clocks: ClockWatch,
                positions: PositionService) -> dict:
    """A Device as the API shows it: its own fields, what the watches last saw of it, and whether
    its GPS Sidecar is connected."""
    return {**device.public(), **certificates.view(device.label), **clocks.view(device.label),
            **positions.view(device.label)}


def services(request: Request) -> Services:
    return request.app.state.services
