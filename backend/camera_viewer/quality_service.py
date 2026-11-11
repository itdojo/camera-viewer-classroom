"""Quality Service: the one way a Camera's Quality changes on its Device (ADR 0009).

Every write goes through ``_apply``: a user's pick (``choose``), an Auto step (``step``, which
the Feed Monitor sends from the Auto controller, ``quality_auto.py``), and a re-apply after the
Device restarted (``sync``). Writes and checks are serialized per Device, each records what it
wrote, when, and the Device's ``started`` at that moment, logs who changed what, and tells
subscribers (the Camera Catalog pushes the ``cameras`` event). Nothing else PATCHes a Device.

Re-apply, and changes by another instance. ``sync`` reads a Device's ``/v3/info`` ``started`` for every
reachable Device every ``check_seconds`` (so within one interval of a Device restarting or coming
back from Offline). It reads each Camera's path config only when ``started`` moved since its last
full check (or this instance has none yet), when the Device was just added, and every
``config_check_seconds`` (60 s), which keeps a running Device to one small read a check and a
change by another instance on a pinned Camera to within a minute. The same comparison runs inside
every write, against configs read just before it. For a Camera this instance has written to:

- ``started`` later than the last write: the Device restarted and forgot it, so the written
  level is put back quietly;
- ``started`` unchanged but the Quality fields differ from the last write: another client
  changed them. The Camera is marked "changed by another instance", Auto leaves it alone and
  nothing is re-applied until a user picks a level or Auto, which takes control back.

A Camera never written to just has its current level read. Picking a level that would drop
viewers while the Camera is Recording splits the Recording, so it needs ``confirm``.

Auto is chosen and stored here; choosing it leaves the level where it is, and the Feed Monitor
moves it from there while the Feed is pulled. ``auto_context`` tells the monitor whether a Camera
is Auto's to step now, and at what level, on which ladder, and whether it is Recording. A write
that drops viewers is announced to ``on_restart`` listeners just before it is sent, so the Feed
Monitor can tell the pull it ends from trouble (spec story 18). Before, not after: a congested
Device's answer queues behind its backlog and can arrive after the pull has already dropped and
come back.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

from camera_viewer.device_client import DeviceClient, DeviceError, DeviceRejected
from camera_viewer.quality import LADDERS, Kit, Ladder, Level, Setting, kit_of, live_setting, patch_for
from camera_viewer.quality_auto import AutoContext
from camera_viewer.quality_store import AUTO, CameraQuality, LastWrite, Mode, QualityStore
from camera_viewer.registry import Camera, Device, DeviceRegistry

log = logging.getLogger("camera_viewer.quality")

By = Literal["user", "auto", "re-apply"]
ClientFor = Callable[[Device], DeviceClient]


class QualityUnavailable(Exception):
    """This Camera's Quality can't be changed now. ``code`` is stable; the message is for people."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SplitsRecording(Exception):
    """The change would drop viewers while the Camera is Recording; it needs a confirm."""


NO_API = QualityUnavailable("no_device_api", "added by RTSP URL, so there is no Device API to set its Quality")
OFFLINE = QualityUnavailable("device_offline", "its Device is Offline")
CERTIFICATE = QualityUnavailable("certificate_changed", "its Device's certificate changed")
UNSUPPORTED = QualityUnavailable("unsupported", "its path on the Device is not a Device Kit camera")


@dataclass(frozen=True)
class _Reading:
    """One Camera's path config as its Device holds it now, with the Device's ``started`` then
    and the client that read them (the one a write that follows uses)."""

    client: DeviceClient
    started: datetime
    config: dict[str, Any]
    kit: Kit | None

    @property
    def ladder(self) -> Ladder:
        return LADDERS[self.kit]

    @property
    def live(self) -> Setting | None:
        return live_setting(self.kit, self.config) if self.kit else None

    @property
    def level(self) -> Level | None:
        return self.ladder.level_of(self.live) if self.kit else None


class QualityService:
    def __init__(self, registry: DeviceRegistry, store: QualityStore, *,
                 reachable: Callable[[str], bool] = lambda label: True,
                 certificate_changed: Callable[[str], bool] = lambda label: False,
                 recording: Callable[[str], bool] = lambda camera_id: False,
                 client_for: ClientFor = DeviceClient.for_device, check_seconds: float = 10.0,
                 config_check_seconds: float = 60.0,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._registry = registry
        self._store = store
        self._reachable = reachable
        self._certificate_changed = certificate_changed
        self._recording = recording
        self._client_for = client_for
        self._check_seconds = check_seconds
        self._config_check_seconds = config_check_seconds
        self._clock = clock
        self._cameras: dict[str, CameraQuality] = store.load()
        self._kits: dict[str, Kit | None] = {}  # what each Camera's path read as last; memory only
        self._errors: dict[str, dict[str, str]] = {}
        # Each Device's `started` at its last full check, and when that was; memory only.
        self._full_checks: dict[str, tuple[datetime, datetime]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._listeners: list[Callable[[], None]] = []
        self._restart_listeners: list[Callable[[str, bool], None]] = []
        registry.subscribe(self._forget_removed)

    # -- wiring -----------------------------------------------------------------------------

    def subscribe(self, listener: Callable[[], None]) -> None:
        """Call ``listener()`` after any Camera's Quality state changes."""
        self._listeners.append(listener)

    def on_restart(self, listener: Callable[[str, bool], None]) -> None:
        """Call ``listener(camera_id, True)`` just before a write that drops the Camera's viewers:
        the Device restarts the source, so the Relay's pull ends and comes back a few seconds
        later. ``listener(camera_id, False)`` follows if the write then fails."""
        self._restart_listeners.append(listener)

    def auto_context(self, camera_id: str) -> AutoContext | None:
        """The Camera's level, ladder and Recording state if Auto may step it now: it is on Auto,
        not "changed by another instance", its menu is available and its level is known."""
        quality = self.get(camera_id)
        kit = self._kits.get(camera_id)
        if not quality.auto or quality.changed_elsewhere or quality.level is None or kit is None:
            return None
        camera = self._registry.camera(camera_id)
        if camera is None:
            return None
        try:
            self._check_available(camera)
        except QualityUnavailable:
            return None
        return AutoContext(level=quality.level, ladder=LADDERS[kit], recording=self._recording(camera_id))

    def get(self, camera_id: str) -> CameraQuality:
        """A Camera's stored Quality (Auto at an unknown level if it has none)."""
        return self._cameras.get(camera_id) or CameraQuality()

    def field(self, camera: Camera) -> dict[str, Any]:
        """The Camera Catalog field this module owns."""
        quality = self.get(camera.id)
        try:
            self._check_available(camera)
            available, reason = True, None
        except QualityUnavailable as exc:
            available, reason = False, str(exc)
        return {"quality": {
            "mode": str(quality.mode),
            "level": str(quality.level) if quality.level else None,
            "available": available,
            "reason": reason,
            "changed_elsewhere": quality.changed_elsewhere,
            "error": self._errors.get(camera.id),
        }}

    # -- changes ----------------------------------------------------------------------------

    async def choose(self, camera_id: str, mode: Mode, *, confirm: bool = False) -> None:
        """A user's pick: Auto, or pin a level. Either takes control back from another instance.
        Raises ``QualityUnavailable``, ``SplitsRecording`` or a ``DeviceError``."""
        camera = self._camera(camera_id)
        async with self._lock(camera.device):
            self._check_available(camera)
            if mode == AUTO:
                await self._choose_auto(camera)
            else:
                await self._apply(camera, mode, by="user", confirm=confirm)

    async def step(self, camera_id: str, level: Level, *, why: str = "") -> bool:
        """An Auto step to ``level``, for the reason ``why`` (logged). Applies only while the
        Camera is on Auto and not "changed by another instance"; returns whether it applied. The
        controller owns the Recording rule (never up across a disruptive step), so no confirm is
        asked here."""
        camera = self._camera(camera_id)
        async with self._lock(camera.device):
            quality = self.get(camera_id)
            if not quality.auto or quality.changed_elsewhere:
                return False
            try:
                self._check_available(camera)
            except QualityUnavailable:
                return False
            return await self._apply(camera, level, by="auto", confirm=True, why=why)

    # -- checking on Devices ----------------------------------------------------------------

    async def run(self) -> None:
        while True:
            try:
                await self.sync_all()
            except Exception:  # one bad pass must never end the checks for good
                log.exception("quality check failed")
            await asyncio.sleep(self._check_seconds)

    async def sync_all(self) -> None:
        devices = [d for d in self._registry.devices()
                   if d.api and d.cameras and self._reachable(d.label) and not self._certificate_changed(d.label)]
        await asyncio.gather(*(self.sync(device.label) for device in devices))

    async def sync(self, label: str, *, read_configs: bool = False) -> None:
        """Read one Device's ``started``. If it moved since the last full check (or this instance
        has none yet), the last full check is ``config_check_seconds`` old, or ``read_configs``
        asks for it (a Device just added), also read its Cameras' configs: re-apply after a
        restart, mark a change by another instance, and note each Camera's current level. Between
        full checks a Device that kept running costs one ``/v3/info`` a check."""
        async with self._lock(label):
            device = self._registry.get(label)
            if device is None or not device.api:
                return
            client = self._client_for(device)
            try:
                started = await client.started()
            except DeviceError as exc:
                log.debug("quality check of %s: %s", label, exc)
                return
            last = self._full_checks.get(label)
            now = self._clock()
            if (not read_configs and last is not None and last[0] == started
                    and (now - last[1]).total_seconds() < self._config_check_seconds):
                return
            read_all = True
            for camera in device.cameras:
                try:
                    await self._sync_camera(camera, await self._read(client, camera, started))
                except DeviceError as exc:
                    read_all = False  # read it again at the next check
                    log.debug("quality check of %s: %s", camera.id, exc)
            if read_all:
                self._full_checks[label] = (started, now)

    # -- internals --------------------------------------------------------------------------

    async def _sync_camera(self, camera: Camera, reading: _Reading) -> None:
        if reading.kit is None:
            return
        quality = self.get(camera.id)
        write = quality.last_write
        if write is None or (quality.changed_elsewhere and reading.started <= write.started):
            self._update(camera.id, replace(quality, level=reading.level))
        elif reading.started > write.started:
            await self._after_restart(camera, reading)
        elif _changed_since(write, reading):
            self._mark_changed_elsewhere(camera, reading)

    async def _after_restart(self, camera: Camera, reading: _Reading) -> None:
        """The Device restarted since this instance's last write, so it forgot it: put it back."""
        quality = self.get(camera.id)
        write = quality.last_write
        wanted = reading.ladder.level_of(write.setting)
        if quality.changed_elsewhere or wanted is None:
            # Not ours to put back; carry on from what the Device booted with.
            self._update(camera.id, replace(quality, level=reading.level))
            return
        if reading.live == write.setting:
            log.info("quality %s: Device restarted, already at %s", camera.id, wanted)
            self._errors.pop(camera.id, None)
            self._update(camera.id, replace(quality, level=wanted,
                                            last_write=replace(write, started=reading.started)))
            return
        await self._apply(camera, wanted, by="re-apply", confirm=True, why="the Device restarted",
                          reading=reading)

    async def _apply(self, camera: Camera, level: Level, *, by: By, confirm: bool, why: str = "",
                     reading: _Reading | None = None) -> bool:
        """Write ``level``, from ``reading`` or a fresh one; returns whether it did. Anything but a
        user's pick first compares the Device with the last write, and writes nothing if another
        instance changed it since."""
        try:
            reading = reading or await self._read_now(camera)
            if reading.kit is None:
                raise UNSUPPORTED
            ladder, live, current = reading.ladder, reading.live, reading.level
            if by != "user" and _changed_since(self.get(camera.id).last_write, reading):
                self._mark_changed_elsewhere(camera, reading)
                return False
            if (not confirm and self._recording(camera.id)
                    and ladder.disrupts(current, level, current_setting=live)):
                raise SplitsRecording(f"changing {camera.id} to {level} drops its viewers for a few seconds, "
                                      "which splits the Recording into a new part")
            target = ladder.setting(level)
            if live != target:
                await self._patch(reading.client, camera, patch_for(reading.kit, level, reading.config),
                                  restarts=ladder.disrupts(current, level, current_setting=live))
        except DeviceError as exc:
            self._fail(camera.id, exc, f"{'re-applying' if by == 're-apply' else 'setting'} {level}")
            raise
        quality = self.get(camera.id)
        log.info("quality %s: %s -> %s (by %s%s)%s", camera.id, current or "unknown", level, by,
                 f": {why}" if why else "", "; Auto off, level pinned" if by == "user" else "")
        self._errors.pop(camera.id, None)
        self._update(camera.id, replace(
            quality, mode=level if by == "user" else quality.mode, level=level, changed_elsewhere=False,
            last_write=LastWrite(setting=target, at=self._clock(), started=reading.started)))
        return True

    def _mark_changed_elsewhere(self, camera: Camera, reading: _Reading) -> None:
        quality = self.get(camera.id)
        log.warning("quality %s: changed by another instance (%s, not the %s this instance set); "
                    "Auto and re-applying stop until a user picks a level", camera.id,
                    _describe(reading.live), _describe(quality.last_write.setting))
        self._update(camera.id, replace(quality, changed_elsewhere=True, level=reading.level))

    async def _choose_auto(self, camera: Camera) -> None:
        quality = self.get(camera.id)
        if quality.changed_elsewhere:
            # Take control back of whatever the other client left: that becomes the last write.
            try:
                reading = await self._read_now(camera)
            except DeviceError as exc:
                self._fail(camera.id, exc, "taking control back")
                raise
            live = reading.live
            write = LastWrite(setting=live, at=self._clock(), started=reading.started) if live else None
            quality = replace(quality, changed_elsewhere=False, last_write=write, level=reading.level)
        log.info("quality %s: Auto on at %s (by user)", camera.id, quality.level or "an unknown level")
        self._errors.pop(camera.id, None)
        self._update(camera.id, replace(quality, mode=AUTO))

    async def _read_now(self, camera: Camera) -> _Reading:
        """The Camera's path and its Device's ``started``, read now, just before a write."""
        client = self._client_for(self._registry.get(camera.device))
        return await self._read(client, camera, await client.started())

    async def _read(self, client: DeviceClient, camera: Camera, started: datetime) -> _Reading:
        try:
            config = await client.path_config(camera.name)
        except DeviceRejected as exc:
            if exc.status != 404:
                raise
            config = {}
        kit = kit_of(config)
        if camera.id not in self._kits or self._kits[camera.id] != kit:
            self._kits[camera.id] = kit
            self._changed()
        return _Reading(client=client, started=started, config=config, kit=kit)

    def _check_available(self, camera: Camera) -> None:
        device = self._registry.get(camera.device)
        if device is None or not device.api:
            raise NO_API
        if not self._reachable(device.label):
            raise OFFLINE
        if self._certificate_changed(device.label):
            raise CERTIFICATE
        if camera.id in self._kits and self._kits[camera.id] is None:
            raise UNSUPPORTED

    def _camera(self, camera_id: str) -> Camera:
        camera = self._registry.camera(camera_id)
        if camera is None:
            raise KeyError(camera_id)
        return camera

    def _lock(self, label: str) -> asyncio.Lock:
        return self._locks.setdefault(label, asyncio.Lock())

    def _fail(self, camera_id: str, exc: DeviceError, doing: str) -> None:
        log.warning("quality %s: %s failed: %s", camera_id, doing, exc)
        self._errors[camera_id] = {"code": exc.code, "message": str(exc)}
        self._changed()

    def _update(self, camera_id: str, quality: CameraQuality) -> None:
        if self._cameras.get(camera_id) == quality:
            return
        self._cameras[camera_id] = quality
        self._store.save(self._cameras)
        self._changed()

    async def _patch(self, client: DeviceClient, camera: Camera, body: dict[str, Any], *, restarts: bool) -> None:
        if restarts:
            self._restarting(camera.id, True)
        try:
            await client.patch_path_config(camera.name, body)
        except DeviceError:
            if restarts:
                self._restarting(camera.id, False)
            raise

    def _restarting(self, camera_id: str, restarting: bool) -> None:
        for listener in self._restart_listeners:
            listener(camera_id, restarting)

    def _changed(self) -> None:
        for listener in self._listeners:
            listener()

    def _forget_removed(self) -> None:
        present = {camera.id for camera in self._registry.cameras()}
        gone = self._cameras.keys() - present
        for camera_id in (self._kits.keys() | self._errors.keys()) - present:
            self._kits.pop(camera_id, None)
            self._errors.pop(camera_id, None)
        for label in self._full_checks.keys() - {device.label for device in self._registry.devices()}:
            del self._full_checks[label]
        if gone:
            for camera_id in gone:
                del self._cameras[camera_id]
            self._store.save(self._cameras)


def _changed_since(write: LastWrite | None, reading: _Reading) -> bool:
    """Whether another client changed the Quality fields since this instance's last write: the
    Device has not restarted since (a restart forgets every write), yet reads otherwise."""
    return write is not None and reading.started <= write.started and reading.live != write.setting


def _describe(setting: Setting | None) -> str:
    if setting is None:
        return "an unreadable setting"
    return f"{setting.width}x{setting.height} {setting.fps} fps {setting.bitrate // 1000} kbit/s"
