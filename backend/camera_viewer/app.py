"""The FastAPI application: builds the modules, wires them together, mounts the
routers under /api and serves the frontend.

``uvicorn camera_viewer.app:create_app --factory`` (the supervisor does this).

Wiring is all in ``build_services``: modules never import each other's
instances, they subscribe. The Device Registry and the Recorder announce
every change; the Relay Controller re-declares its paths from both, and the
event bus pushes the new ``devices``, ``cameras`` and ``recordings`` state to
every browser. The Feed Monitor adds ``feed_status`` to the Camera list and
republishes it on every change, and drives Auto Quality through the Quality
Service. The Disk Monitor pushes ``disk`` after every
measurement and runs the low-disk cutoff through the Recorder. The Position
Service pushes ``positions`` from each Device's GPS Sidecar and its Fixed
Position, and ``fixed_positions`` for the Devices Panel. The Map Extract is
chosen once at startup and announced in ``settings``.

``create_app`` wraps the whole app in ``auth.SessionGate``, so a router added
to ``ROUTERS`` (or anything else mounted) is behind the session without doing
anything. Only ``auth.PUBLIC`` is reachable without one.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import secrets
import shlex
import sys
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from camera_viewer.api import cameras, devices, disk, errors, feeds, maps, pinning, recordings, session, snapshots
from camera_viewer.api import captures as captures_api
from camera_viewer.api import events as events_api
from camera_viewer.api import fixed_positions as fixed_positions_api
from camera_viewer.api import position_demand as position_demand_api
from camera_viewer.api import trails as trails_api
from camera_viewer.auth import Auth, LoginThrottle, SessionGate, load_secret
from camera_viewer.body_limit import BodyLimit
from camera_viewer.cameras import CameraCatalog
from camera_viewer.captures import CaptureStore
from camera_viewer.certificates import REASON as CERTIFICATE_CHANGED
from camera_viewer.certificates import CertificateWatch
from camera_viewer.clocks import REASON as CLOCK_WRONG
from camera_viewer.clocks import ClockWatch
from camera_viewer.config import Settings, check
from camera_viewer.device_store import DeviceStore
from camera_viewer.disk import DiskMonitor
from camera_viewer.events import EventBus
from camera_viewer.feed_monitor import FeedMonitor
from camera_viewer.maps import find_extract, map_settings
from camera_viewer.playback import PlaybackGateway
from camera_viewer.position_store import PositionStore
from camera_viewer.positions import PositionService
from camera_viewer.quality_service import QualityService
from camera_viewer.quality_store import QualityStore
from camera_viewer.recorder import EndReason, Recorder, State
from camera_viewer.recording_store import RecordingStore
from camera_viewer.registry import DeviceRegistry
from camera_viewer.relay import RecordSettings, RelayController, RelayCredentials, paths_for, video_size
from camera_viewer.services import Services, device_view
from camera_viewer.timers import RecordingTimers

# The image's Python (3.13) has no woff2 type in its table, so the bundled
# fonts (#63) would go out as application/octet-stream.
mimetypes.add_type("font/woff2", ".woff2")


class Frontend(StaticFiles):
    """The frontend files, each marked ``no-cache``: a browser revalidates
    them on every load (a cheap 304 by ETag) instead of reusing a module from
    an older release against a newer page, which once left the Add Device form
    with no submit handler."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


ROUTERS = [session.router, devices.router, pinning.router, cameras.router, feeds.router,
           recordings.router, snapshots.router, captures_api.router, disk.router, maps.router, events_api.router,
           fixed_positions_api.router, trails_api.router, position_demand_api.router]


def hook_url(settings: Settings) -> str:
    """Where the Relay's segment hooks reach this backend: its own UI listener,
    over HTTPS when the UI has a certificate, at the loopback address when it
    listens on every interface or on loopback, else at the one address it
    listens on."""
    host = settings.http_host.strip()
    if host in ("", "0.0.0.0", "localhost") or host.startswith("127."):
        host = "127.0.0.1"
    elif host in ("::", "[::]", "::1", "[::1]"):
        host = "[::1]"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    scheme = "https" if settings.tls else "http"
    return f"{scheme}://{host}:{settings.http_port}{recordings.hook_router.prefix}/segments"


def record_settings(settings: Settings, store: CaptureStore, token: str) -> RecordSettings:
    """Where the Relay writes segments, and the hook commands that report them."""
    url = hook_url(settings)

    def hook(event: str) -> str:
        return shlex.join([sys.executable, "-m", "camera_viewer.hook", url, token, event])

    return RecordSettings(path=f"{store.staging}/%path/%Y%m%d-%H%M%S-%f",
                          on_segment_create=hook("create"), on_segment_complete=hook("complete"))


def build_services(settings: Settings) -> Services:
    auth = Auth(settings.password, load_secret(settings.config_dir), settings.session_max_age_seconds,
                throttle=LoginThrottle(settings.login_max_failures, settings.login_lockout_seconds))
    events = EventBus()
    registry = DeviceRegistry(DeviceStore(settings.config_dir))
    relay_user = RelayCredentials(settings.relay_user, settings.relay_pass)
    relay = RelayController(settings.relay_api_url, interval=settings.relay_reconcile_seconds,
                            credentials=relay_user)
    playback = PlaybackGateway(relay, settings.relay_whep_url, credentials=relay_user)
    certificates = CertificateWatch(registry, interval=settings.certificate_check_seconds)
    clocks = ClockWatch(registry)
    positions = PositionService(registry, settings.position_timings, store=PositionStore(settings.config_dir))
    store = CaptureStore(settings.captures_dir)
    hook_token = secrets.token_urlsafe(24)
    recording = record_settings(settings, store, hook_token)

    async def received_resolution(camera_id: str) -> tuple[int, int] | None:
        try:
            return video_size(await relay.path_state(camera_id))
        except httpx.HTTPError:
            return None

    # A stopping Recording waits until the Relay has applied "record off" for its path.
    recorder = Recorder(store, received_resolution, journal=RecordingStore(settings.config_dir),
                        record_off=relay.settled)
    RecordingTimers(recorder)  # subscribes itself: ends timed Recordings on time

    def device_list():
        return [device_view(device, certificates, clocks, positions) for device in registry.devices()]

    def recording_list():
        return [r.public() for r in recorder.unfinished()]

    def declare_paths():
        relay.declare(paths_for(registry.devices(), recording=recording,
                                recorded=recorder.recorded(), held=recorder.held(), relay=relay_user))

    def recording_field(camera):
        current = recorder.for_camera(camera.id)
        return {"recording": current.public() if current else None}

    events.register_snapshot("devices", device_list)
    events.register_snapshot("recordings", recording_list)
    # The Map Extract is picked once, at startup, so every browser draws the same extract.
    map_extract = find_extract(settings.config_dir)
    # Settings the browser applies itself; fixed for the instance's lifetime. `map` says where
    # each basemap is (camera_viewer.maps).
    events.register_snapshot("settings", lambda: {"stall_seconds": settings.stall_seconds,
                                                  "map": map_settings(map_extract, settings.map_tiles,
                                                                      settings.map_imagery)})
    registry.subscribe(lambda: events.publish("devices", device_list()))
    registry.subscribe(declare_paths)
    # The Relay keeps API config in memory only: declare the stored Devices'
    # paths now, and the reconcile loop applies them whenever the Relay starts.
    declare_paths()
    catalog = CameraCatalog(registry, events)
    catalog.add_field_provider(certificates.camera_fields)
    catalog.add_field_provider(recording_field)
    certificates.subscribe(lambda: events.publish("devices", device_list()))
    certificates.subscribe(catalog.publish)
    clocks.subscribe(lambda: events.publish("devices", device_list()))
    # Positions: the whole map on every accepted fix and staleness change; `devices` only when a
    # Device's GPS Sidecar connects or is lost.
    events.register_snapshot("positions", positions.positions)
    positions.subscribe(lambda: events.publish("positions", positions.positions()))
    positions.on_sidecar_change(lambda: events.publish("devices", device_list()))
    # Fixed Positions for the Devices Panel, on their own key so a write never re-publishes `devices`.
    events.register_snapshot("fixed_positions", positions.fixed_positions)
    positions.on_fixed_change(lambda: events.publish("fixed_positions", positions.fixed_positions()))
    recorder.subscribe(declare_paths)
    # Removal waits (at most the Recorder's stop timeout) until the Device's
    # Recordings are finalized, so the Relay closes their files while it still
    # has the Device's paths. A Recording started on one of its Cameras during
    # that wait is ended once the Camera is gone.
    registry.on_removed(lambda device: recorder.finish(device.cameras, EndReason.DEVICE_REMOVED))

    def end_orphaned_recordings():
        cameras = {camera.id for camera in registry.cameras()}
        recorder.end_recordings([r.camera for r in recorder.unfinished() if r.camera.id not in cameras],
                                EndReason.DEVICE_REMOVED)

    registry.subscribe(end_orphaned_recordings)
    recorder.subscribe(lambda: events.publish("recordings", recording_list()))
    recorder.subscribe(catalog.publish)

    # Captures: the folder's contents, plus the Recorder's Interrupted and
    # unfinished flags. The Recorder changes far more often than the list, so
    # only a list that differs from the last one is pushed.
    def capture_list():
        return captures_api.capture_list(store, recorder)

    published_captures: list = [None]

    def publish_captures():
        current = capture_list()
        if current != published_captures[0]:
            published_captures[0] = current
            events.publish("captures", current)

    events.register_snapshot("captures", capture_list)
    store.subscribe(publish_captures)
    recorder.subscribe(publish_captures)

    # A Device problem makes every Feed of a reachable Device Down. A changed certificate
    # outranks a wrong clock: the clock is read through the pin, so it goes unread meanwhile.
    def device_problem(label: str) -> str | None:
        if certificates.changed(label):
            return CERTIFICATE_CHANGED
        if clocks.wrong(label):
            return CLOCK_WRONG
        return None

    monitor = FeedMonitor(registry, relay, thresholds=settings.feed_thresholds,
                          sample_seconds=settings.feed_sample_seconds,
                          probe_seconds=settings.device_probe_seconds,
                          problem=device_problem)
    monitor.add_demand(playback.opening)
    monitor.add_demand(lambda camera_id: camera_id in recorder.held())  # a Recording wants its Feed too
    catalog.add_field_provider(monitor.field)
    monitor.subscribe(catalog.publish)

    # Quality: the one write path to a Device's encoder settings (ADR 0009). Its menu follows
    # Offline and "certificate changed", so the Camera list is re-published on either.
    quality = QualityService(registry, QualityStore(settings.config_dir),
                             reachable=monitor.device_reachable, certificate_changed=certificates.changed,
                             recording=lambda camera_id: recorder.for_camera(camera_id) is not None,
                             check_seconds=settings.quality_check_seconds,
                             config_check_seconds=settings.quality_config_check_seconds)
    catalog.add_field_provider(quality.field)
    quality.subscribe(catalog.publish)
    # Auto reads the Feed Monitor's samples and steps through the Quality Service; a write that
    # restarts a Camera's source tells the monitor, so the pull it ends never reads as trouble.
    monitor.drive_quality(quality, settings.quality_thresholds)
    quality.on_restart(monitor.quality_restarting)

    # The low-disk cutoff: below the finalize threshold, every Recording not
    # already stopping is ended, so the Relay closes its file properly.
    space = DiskMonitor(settings.captures_dir, settings.disk_thresholds, interval=settings.disk_check_seconds)
    events.register_snapshot("disk", lambda: space.current().public())
    space.subscribe(lambda reading: events.publish("disk", reading.public()))
    space.on_cutoff(lambda: recorder.end_recordings(
        [r.camera for r in recorder.unfinished() if r.state is not State.STOPPING], EndReason.LOW_DISK))

    return Services(settings=settings, auth=auth, events=events, registry=registry, cameras=catalog,
                    relay=relay, playback=playback,
                    certificates=certificates, clocks=clocks, captures=store, recorder=recorder,
                    hook_token=hook_token, feeds=monitor, disk=space, quality=quality,
                    positions=positions, map_extract=map_extract)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    check(settings)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # the reconcile loop polls every interval
    svc = build_services(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc.captures.prepare()
        svc.recorder.recover()  # Recordings the last run left unfinished become Interrupted
        tasks = [asyncio.create_task(svc.relay.run(), name="relay-reconcile"),
                 asyncio.create_task(svc.certificates.run(), name="certificate-watch"),
                 asyncio.create_task(svc.clocks.run(), name="clock-watch"),
                 asyncio.create_task(svc.feeds.run(), name="feed-monitor"),
                 asyncio.create_task(svc.disk.run(), name="disk-monitor"),
                 asyncio.create_task(svc.quality.run(), name="quality-check"),
                 asyncio.create_task(svc.positions.run(), name="position-service")]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            await svc.playback.aclose()
            await svc.relay.aclose()

    app = FastAPI(title="Camera Viewer", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.services = svc
    errors.install(app)
    for router in ROUTERS:
        app.include_router(router, prefix="/api")
    app.include_router(session.pages)
    app.include_router(recordings.hook_router)  # the Relay's, not the browser's: token, no session
    app.mount("/", Frontend(directory=settings.frontend_dir, html=True), name="frontend")
    # Inside the SessionGate, so a request without a session is refused before its body is read.
    app.add_middleware(BodyLimit)
    # Deny by default: every route, mount and stream, present and future,
    # needs a session, except the login page and endpoint (auth.PUBLIC).
    app.add_middleware(SessionGate, auth=svc.auth)
    return app
