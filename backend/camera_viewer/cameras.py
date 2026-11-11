"""Camera Catalog: the Camera list as browsers see it.

Joins what several modules know about each Camera into one view. The base
view comes from the Device Registry (id, Device, name, resolution, and
whether its source is encrypted: ``rtsps://``, not plain ``rtsp://``). Other
modules add their own fields by registering a field provider instead of
editing this file: the Feed Monitor adds ``feed_status``, the Recorder adds
``recording``. A provider takes a Camera and returns a dict merged into its
view; later providers win on a key clash.

Whoever changes something a provider reports calls ``publish()``, which
pushes the whole list to every browser as a ``cameras`` event.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from camera_viewer.events import EventBus
from camera_viewer.registry import Camera, DeviceRegistry

FieldProvider = Callable[[Camera], dict[str, Any]]

EVENT = "cameras"


def base_view(camera: Camera) -> dict[str, Any]:
    resolution = None
    if camera.width and camera.height:
        resolution = {"width": camera.width, "height": camera.height}
    return {"id": camera.id, "device": camera.device, "name": camera.name, "resolution": resolution,
            "encrypted": camera.source_url.lower().startswith("rtsps://")}


class CameraCatalog:
    def __init__(self, registry: DeviceRegistry, events: EventBus) -> None:
        self._registry = registry
        self._events = events
        self._providers: list[FieldProvider] = [base_view]
        events.register_snapshot(EVENT, self.list)
        registry.subscribe(self.publish)

    def add_field_provider(self, provider: FieldProvider) -> None:
        self._providers.append(provider)

    def list(self) -> list[dict[str, Any]]:
        views = []
        for camera in self._registry.cameras():
            view: dict[str, Any] = {}
            for provider in self._providers:
                view.update(provider(camera))
            views.append(view)
        return views

    def get(self, camera_id: str) -> dict[str, Any] | None:
        return next((view for view in self.list() if view["id"] == camera_id), None)

    def publish(self) -> None:
        self._events.publish(EVENT, self.list())
