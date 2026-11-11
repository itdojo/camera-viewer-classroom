"""Feed Monitor: samples the Relay and every Device, and publishes each Feed's Feed Status.

Two loops. The probe loop checks every Device's reachability (a TCP connect to its API port,
through the Device Client). The sample loop reads each Camera's path and static source from
the Relay, through the Relay Controller, and runs the Feed Status Evaluator. Whenever any
Feed Status changes, or the resolution the Relay receives (a Quality change), the Camera Catalog
pushes the whole Camera list to every browser.

Demand. A Feed is pulled while something wants it: the Relay has a reader on its path, or a
demand source (the Playback Gateway's offers in flight; later a Recording's hold) says so.
Demand lingers for ``DEMAND_LINGER_SECONDS`` after it was last seen, because an on-demand
source that fails makes the Relay drop its readers at once: without the linger, a Feed whose
Device died would read Idle instead of Down while its viewer retries. Each time a Feed gains
demand its sample series starts afresh, so a Feed being opened reads as starting, never as
failing for however long it sat idle.

Offline is debounced: a Device is unreachable only once its probes have failed for the Down
threshold, so one lost probe on a flaky link is no alarm (spec story 29). A Device made from
a manual Camera (``api: false``) has no API to probe, so it is never Offline; its Feed Status
comes from the Relay alone.

A Device problem (the ``problem`` callable: the Certificate Watch's "certificate changed",
or the Clock Watch's "Device clock wrong") makes every Feed of a reachable Device Down, with the problem as
``feed_status_reason``, whether or not anyone is pulling it.

Auto Quality (ADR 0009). Once ``drive_quality`` has wired in the Quality Service, every new sample
of a pulled Camera on Auto goes to the Auto controller (``quality_auto.decide``), whose state is
kept here in memory, and any step it takes is sent to the Quality Service, the one way a Quality
changes. One step at a time per Camera: the next decision waits until the write is done.

A Quality write that drops viewers (``quality_restarting``; every ffmpeg step, CSI's 720p/1080p
one) makes the Device restart the source. The Relay's pull ends once the Device has drained what it
had queued of the old stream (within a second, or several behind a slow uplink) and comes back
about 5 s later on the Relay's retry. That is a choice, not trouble (spec story 18), so until the
source is back and receiving, the Feed keeps the Feed Status it had and Auto waits: at most
``RESTART_DROP_SECONDS`` for the pull to drop, then ``RESTART_GRACE_SECONDS`` for it to return.
Then the Feed is judged from a fresh series, as a new pull is, and Auto judges the new level from
there. A source that never comes back reads Down once the grace is over.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from camera_viewer.device_client import DeviceClient, dimension
from camera_viewer.feed_status import FeedStatus, RelaySample, Thresholds, evaluate_feed
from camera_viewer.quality import Level
from camera_viewer.quality_auto import AutoContext, AutoState, AutoThresholds, Step, decide
from camera_viewer.registry import Camera, Device, DeviceRegistry
from camera_viewer.relay import RelayController, video_size

log = logging.getLogger("camera_viewer.feed_monitor")

DEMAND_LINGER_SECONDS = 15.0
PROBE_TIMEOUT_SECONDS = 2.0
# A Quality write that restarts a Camera's source: how long the old stream may keep arriving before
# the pull drops, and then how long the source may take to come back, before the Feed is judged as
# usual again. Measured on the test-pattern kit (2026-10-15): unthrottled, the pull is gone within
# 0.3 s of the write and back 5 s later (the Relay's retry); behind a 600 kbit uplink the Device's
# queued backlog keeps arriving for 4.7 s first, and a slower link drains it slower still.
RESTART_DROP_SECONDS = 30.0
RESTART_GRACE_SECONDS = 15.0

Demand = Callable[[str], bool]
Probe = Callable[[Device], Awaitable[bool]]
Problem = Callable[[str], str | None]


class AutoQuality(Protocol):
    """The Quality Service, as Auto uses it (``quality_service.QualityService``)."""

    def auto_context(self, camera_id: str) -> AutoContext | None: ...
    async def step(self, camera_id: str, level: Level, *, why: str = "") -> bool: ...


async def tcp_probe(device: Device) -> bool:
    return await DeviceClient(device.address, timeout=PROBE_TIMEOUT_SECONDS).reachable()


@dataclass
class _Feed:
    status: FeedStatus = FeedStatus.IDLE
    reason: str | None = None
    demand_seen_at: float | None = None
    samples: list[RelaySample] = field(default_factory=list)
    resolution: tuple[int, int] | None = None  # as last received by the Relay
    auto: AutoState = field(default_factory=AutoState)
    stepping: bool = False  # an Auto step is being written
    restart_until: float | None = None  # a Quality write restarted the source; hold until then
    restart_dropped: bool = False  # the restart's drop has been seen


@dataclass
class _Reachability:
    failing_since: float | None = None


class FeedMonitor:
    def __init__(self, registry: DeviceRegistry, relay: RelayController, *,
                 thresholds: Thresholds = Thresholds(), sample_seconds: float = 1.0,
                 probe_seconds: float = 2.0, probe: Probe = tcp_probe,
                 problem: Problem = lambda label: None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._registry = registry
        self._relay = relay
        self._thresholds = thresholds
        self._sample_seconds = sample_seconds
        self._probe_seconds = probe_seconds
        self._probe = probe
        self._problem = problem
        self._clock = clock
        self._demand: list[Demand] = []
        self._feeds: dict[str, _Feed] = {}
        self._devices: dict[str, _Reachability] = {}
        self._listeners: list[Callable[[], None]] = []
        self._quality: AutoQuality | None = None
        self._auto_thresholds = AutoThresholds()
        self._tasks: set[asyncio.Task] = set()
        self._set_history()

    def _set_history(self) -> None:
        # Keep the longer look-back (Down or Degraded) plus one Degraded window of slack, so the
        # sample just before a window opens is still in hand; and two of Auto's windows, since it
        # judges each window from the sample that opened it.
        t = self._thresholds
        self._history = max(max(t.down_after_seconds, t.degraded_window_seconds) + t.degraded_window_seconds,
                            2 * self._auto_thresholds.window_seconds)

    # -- wiring -----------------------------------------------------------------

    def add_demand(self, demand: Demand) -> None:
        """Register something that can want a Feed pulled: ``demand(camera_id) -> bool``."""
        self._demand.append(demand)

    def drive_quality(self, quality: AutoQuality, thresholds: AutoThresholds = AutoThresholds()) -> None:
        """Run Auto on every pulled Camera the Quality Service says is Auto's to step."""
        self._quality = quality
        self._auto_thresholds = thresholds
        self._set_history()

    def quality_restarting(self, camera_id: str, restarting: bool = True) -> None:
        """A Quality write is about to make the Camera's Device restart its source (see the module
        doc); ``restarting`` False means the write failed after all, so nothing restarts."""
        feed = self._feeds.setdefault(camera_id, _Feed())
        feed.restart_until = self._clock() + RESTART_DROP_SECONDS if restarting else None
        feed.restart_dropped = False

    def subscribe(self, listener: Callable[[], None]) -> None:
        """Call ``listener()`` after any Feed Status changes."""
        self._listeners.append(listener)

    def field(self, camera: Camera) -> dict[str, Any]:
        """The Camera Catalog fields this module owns. ``resolution`` is the one the Relay last
        received, once it has received any, so a Quality change shows; until then the Camera
        keeps the one its Device reported when it was added."""
        feed = self._feeds.get(camera.id) or _Feed()
        fields: dict[str, Any] = {"feed_status": feed.status.value, "feed_status_reason": feed.reason}
        if feed.resolution:
            fields["resolution"] = {"width": feed.resolution[0], "height": feed.resolution[1]}
        return fields

    def device_reachable(self, label: str) -> bool:
        """Whether a Device counts as reachable now: False only once it reads Offline."""
        return self._reachable(label, self._clock())

    # -- loops ------------------------------------------------------------------

    async def run(self) -> None:
        await asyncio.gather(self._every(self._probe_seconds, self.probe_devices),
                             self._every(self._sample_seconds, self.sample_feeds))

    async def probe_devices(self) -> None:
        devices = [device for device in self._registry.devices() if device.api]
        results = await asyncio.gather(*(self._probe(device) for device in devices),
                                       return_exceptions=True)
        now = self._clock()
        for device, result in zip(devices, results, strict=True):
            state = self._devices.setdefault(device.label, _Reachability())
            if result is True:
                state.failing_since = None
            elif state.failing_since is None:
                state.failing_since = now
        for label in self._devices.keys() - {device.label for device in devices}:
            del self._devices[label]
        self._evaluate_all()

    async def sample_feeds(self) -> None:
        cameras = self._registry.cameras()
        try:
            states = await asyncio.gather(*(self._read_relay(camera.id) for camera in cameras))
        except (httpx.HTTPError, OSError) as exc:
            log.debug("relay not readable this tick: %r", exc)
            return
        now = self._clock()
        resized = False
        for camera, (path, source) in zip(cameras, states, strict=True):
            feed = self._feeds.setdefault(camera.id, _Feed())
            size = _received_size(path)
            if size and size != feed.resolution:
                feed.resolution = size
                resized = True
            readers = bool(path and path.get("readers"))
            if readers or any(demand(camera.id) for demand in self._demand):
                if not self._in_demand(feed, now):
                    feed.samples = []  # a new pull: start a fresh series
                    feed.auto = AutoState(wait=feed.auto.wait)
                feed.demand_seen_at = now
            if self._in_demand(feed, now):
                feed.samples.append(_sample(now, path, source))
                feed.samples = [s for s in feed.samples if now - s.at <= self._history]
                if feed.restart_until is not None:
                    self._follow_restart(feed, now)
                else:
                    self._drive_auto(camera, feed, now)
            else:
                feed.samples = []
                feed.restart_until = None
        for camera_id in self._feeds.keys() - {camera.id for camera in cameras}:
            del self._feeds[camera_id]
        self._evaluate_all(changed=resized)

    # -- internals ----------------------------------------------------------------

    def _follow_restart(self, feed: _Feed, now: float) -> None:
        """While a Quality write's restart lasts: note the drop, and once the source is receiving
        again start the Feed's series afresh from that sample, as for a new pull."""
        latest = feed.samples[-1]
        before = feed.samples[-2] if len(feed.samples) > 1 else None
        if not feed.restart_dropped and (latest.failing or (before and latest.inbound_bytes < before.inbound_bytes)):
            feed.restart_dropped = True  # the pull is gone, or a new source has started
            feed.restart_until = now + RESTART_GRACE_SECONDS
        if feed.restart_dropped and before and not latest.failing and latest.inbound_bytes > before.inbound_bytes:
            feed.samples = [latest]
            feed.restart_until = None
        if feed.restart_until is not None and now >= feed.restart_until:
            feed.restart_until = None
        if feed.restart_until is None:
            feed.auto = feed.auto.restarted(now)

    def _drive_auto(self, camera: Camera, feed: _Feed, now: float) -> None:
        if self._quality is None or feed.stepping:
            return
        if not self._reachable(camera.device, now) or self._problem(camera.device):
            return
        context = self._quality.auto_context(camera.id)
        if context is None:
            feed.auto = AutoState()
            return
        feed.auto, step = decide(feed.samples, feed.auto, context, thresholds=self._auto_thresholds,
                                 feed=self._thresholds)
        if step is not None:
            feed.stepping = True
            task = asyncio.create_task(self._step(camera.id, feed, step))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _step(self, camera_id: str, feed: _Feed, step: Step) -> None:
        try:
            await self._quality.step(camera_id, step.level, why=step.why)
        except Exception as exc:  # a refusal is already on the Camera's quality.error
            log.warning("auto step of %s to %s failed: %s", camera_id, step.level, exc)
        finally:
            feed.stepping = False

    def _in_demand(self, feed: _Feed, now: float) -> bool:
        return feed.demand_seen_at is not None and now - feed.demand_seen_at < DEMAND_LINGER_SECONDS

    def _reachable(self, label: str, now: float) -> bool:
        state = self._devices.get(label)
        return (state is None or state.failing_since is None
                or now - state.failing_since < self._thresholds.down_after_seconds)

    def _evaluate_all(self, *, changed: bool = False) -> None:
        now = self._clock()
        for camera in self._registry.cameras():
            feed = self._feeds.setdefault(camera.id, _Feed())
            reachable = self._reachable(camera.device, now)
            reason = self._problem(camera.device) if reachable else None
            if reason:
                status = FeedStatus.DOWN
            elif reachable and feed.restart_until is not None:
                status = feed.status  # a chosen restart is no fault: hold until the source is back
            else:
                status = evaluate_feed(feed.samples, device_reachable=reachable,
                                       pulled=self._in_demand(feed, now), thresholds=self._thresholds)
            if (status, reason) != (feed.status, feed.reason):
                log.info("feed %s: %s -> %s%s", camera.id, feed.status.value, status.value,
                         f" ({reason})" if reason else "")
                feed.status, feed.reason = status, reason
                changed = True
        if changed:
            for listener in self._listeners:
                listener()

    async def _read_relay(self, camera_id: str) -> tuple[dict | None, dict | None]:
        path = await self._relay.path_state(camera_id)
        source = await self._relay.source_state(camera_id) if path else None
        return path, source

    async def _every(self, seconds: float, tick: Callable[[], Any]) -> None:
        while True:
            try:
                await tick()
            except Exception:  # one bad tick must never stop Feed Status for good
                log.exception("feed monitor tick failed")
            await asyncio.sleep(seconds)


def _received_size(path: dict | None) -> tuple[int, int] | None:
    """The resolution a ready path is receiving, if it reads as a sane one."""
    if not path or not path.get("available", path.get("ready")):
        return None
    try:
        size = video_size(path)
    except (TypeError, ValueError, AttributeError):
        return None
    return size if size and all(dimension(n) for n in size) else None


def _sample(now: float, path: dict | None, source: dict | None) -> RelaySample:
    """One Evaluator sample from the Relay's path (``/v3/paths/get``) and static source
    (``/v3/paths/static-sources/get``) state; either may be missing."""
    path = path or {}
    source = source or {}
    rtp = source.get("typeSpecific") or {}
    return RelaySample(
        at=now,
        # `available`, not `online`: on 1.21.2 a path whose pulled source stopped keeps
        # reporting `online: true` (with `available: false`).
        ready=bool(path.get("available", path.get("ready", False))),
        inbound_bytes=int(path.get("inboundBytes", path.get("bytesReceived", 0)) or 0),
        source_error=source.get("state") == "error",
        frames_in_error=int(path.get("inboundFramesInError", 0) or 0),
        rtp_packets=rtp.get("inboundRTPPackets"),
        rtp_packets_lost=rtp.get("inboundRTPPacketsLost"),
    )
