"""Position Service: owns every Device's Position (#53, ADR 0010).

A Position is where a Device is: a live fix that the Device's GPS Sidecar streams, or a Fixed
Position entered by hand (#58). The Trail (#60) keeps its recent fixes, and Position Demand (#64)
sets how often they come.

Probing and streaming. For every Device with an API, the service opens the Sidecar's stream,
``GET :9998/v1/fixes?interval=<s>``, through ``DeviceClient.stream``: the Device's own host, its
pinned certificate checked inside the handshake before the credentials go out (CODING_STANDARDS
rule 2). While no Sidecar answers it tries again every ``probe_seconds``; once one has answered
and the stream drops, it reconnects with a doubling backoff that starts at a second and never
waits longer than the probe interval. A Device added (or re-pinned) is tried at once, so a Device
whose Sidecar is already up has a Position within a second or two.

Rate (ADR 0012). The stream asks the Sidecar for one fix per ``idle_seconds`` unless the Device
has live Position Demand: a browser with one of its Cameras in Single View renews it (``renew``),
and while the last renewal is younger than ``demand_linger_seconds`` the stream runs at one fix a
second. A change of rate, either way, reopens the stream with the new interval, at once on a
renewal and within a tick of the linger running out.

Validation. Every line passes ``check_fix`` before it touches state (rule 1): a Device is
untrusted input, and a student's Device can run anything on 9998. A refused line is dropped and
logged at most once per Device per minute, with how many were refused since the last report.

Which Position is shown. A fix younger than the staleness threshold (below) wins; otherwise the Device's Fixed
Position, if it has one; otherwise its last fix, marked stale. A Fixed Position is checked with the
fixes' own latitude and longitude rules (``check_place``), kept in the positions store beside the
Quality store (``position_store``), and dropped, from the store too, when its Device is removed.

Trail. Every accepted fix also joins its Device's Trail, an in-memory ring buffer of the fixes that
arrived in the last ``trail_seconds`` (#60). Only accepted fixes enter it, never a Fixed Position,
and it goes with its Device. Nothing of it is persisted, and it never travels over SSE: ``trail``
answers the Device's trail resource, which the map reads once when it opens.

Staleness. A fix is judged by when it arrived, never by its GPS time, which jumps back at every
loop of a replayed track (ADR 0010). A Position whose last fix is ``stale_seconds`` old is stale;
it keeps its coordinates and carries its age. At the idle interval fixes are that far apart by
design, so there the threshold is ``idle_seconds + stale_seconds``, counted at the slower of the
rate the fix arrived at and the rate now: a Device renewed or lapsed a moment ago is not stale
before the new rate's first fix has had time to come.

What it tells others. ``positions()`` is the ``positions`` store key: each Device label to its
Position. Listeners added with ``subscribe`` hear of every accepted fix, of a Position crossing
the staleness threshold, of a Fixed Position set or cleared, and of a Position dropped with its
Device; ``on_fixed_change`` listeners hear only of Fixed Positions (``fixed_positions()``). ``view(label)`` adds
``gps_sidecar`` (a stream is open) to the Device's API view, and ``on_sidecar_change`` listeners
hear only when that flips.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from camera_viewer.device_client import GPS_SIDECAR_PORT, DeviceClient, DeviceError
from camera_viewer.registry import Device, DeviceRegistry

log = logging.getLogger("camera_viewer.positions")

# The stream's interval while the Device has live Position Demand (the idle one is a timing).
FAST_INTERVAL_SECONDS = 1.0
# How much longer than the interval a read may wait. A silent receiver sends nothing (ADR 0010),
# so this also fires then, and the reconnect it causes is harmless.
READ_SLACK_SECONDS = 10.0
BACKOFF_FIRST_SECONDS = 1.0
REFUSAL_LOG_SECONDS = 60.0
TICK_SECONDS = 0.5  # how often staleness is checked
# A Trail holds at most this many fixes per second of its length, so a hostile Sidecar that streams
# far faster than it was asked to can't grow one without bound (CODING_STANDARDS rule 1).
TRAIL_FIXES_PER_SECOND = 2

MAX_LINE = 1024  # bytes, as the Sidecar's own cap
SPEED_MAX = 300.0  # m/s (1080 km/h): nothing a Camera rides on goes faster
ALTITUDE_RANGE = (-1000.0, 100_000.0)  # metres
HDOP_MAX = 100.0
SATELLITES_MAX = 255
GPS_TIME_MAX = 40  # characters
LATITUDE = (-90.0, 90.0)  # degrees
LONGITUDE = (-180.0, 180.0)
FIX_FIELDS = ("time", "lat", "lon", "alt", "speed", "track", "mode", "satellites", "hdop")


@dataclass(frozen=True)
class PositionTimings:
    """The Position Service's timings, each a ``POSITION_<FIELD>`` variable (``config``)."""

    stale_seconds: float = 10.0  # a fix older than this makes the Position stale
    probe_seconds: float = 30.0  # how often a Device with no Sidecar found is tried again
    trail_seconds: float = 600.0  # how far back a Device's Trail reaches
    idle_seconds: float = 30.0  # the stream's interval while the Device has no Position Demand
    demand_linger_seconds: float = 25.0  # Position Demand lasts this long after its last renewal


# -- the validator seam ----------------------------------------------------------------------


class InvalidFix(ValueError):
    """A line from a Sidecar that must not become a Position. The message says why, for the log."""


@dataclass(frozen=True)
class Fix:
    """One accepted fix. Optional fields are None when the receiver did not provide them."""

    latitude: float
    longitude: float
    altitude: float | None
    speed: float | None      # m/s
    heading: float | None    # degrees true
    mode: str                # "2d" or "3d", the Sidecar's own field name
    satellites: int | None
    hdop: float | None
    gps_time: str | None     # ISO 8601 UTC, milliseconds, "Z"


def _no_constant(name: str) -> Any:
    raise InvalidFix(f"{name} is not a number")


def _number(data: dict, key: str, low: float, high: float, *, required: bool = False) -> float | None:
    value = data[key]
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidFix(f"{key} is not a number: {str(value)[:20]!r}")
    try:
        number = float(value)
    except OverflowError:  # an integer of hundreds of digits
        raise InvalidFix(f"{key} is out of range") from None
    if not math.isfinite(number) or not low <= number <= high:
        raise InvalidFix(f"{key} is out of range: {number!r}")
    return number


def _gps_time(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > GPS_TIME_MAX:
        raise InvalidFix("time is not an ISO 8601 time")
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidFix(f"time is not an ISO 8601 time: {value!r}") from None
    if when.tzinfo is None:
        raise InvalidFix(f"time has no time zone: {value!r}")
    when = when.astimezone(UTC)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def check_fix(line: bytes) -> Fix:
    """The one check every line from a GPS Sidecar passes before it touches state
    (CODING_STANDARDS rule 1). Raises ``InvalidFix`` for a line over ``MAX_LINE`` bytes, one
    that is not a JSON object with every fix field, any number that is not finite, a latitude
    outside ±90 or a longitude outside ±180, a fix below 2D, a speed below zero or above
    ``SPEED_MAX``, a track outside 0 to 360, or a value of the wrong kind."""
    if len(line) > MAX_LINE:
        raise InvalidFix(f"a line of more than {MAX_LINE} bytes")
    try:
        data = json.loads(line, parse_constant=_no_constant)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, InvalidFix):
            raise
        raise InvalidFix("not JSON") from None
    if not isinstance(data, dict):
        raise InvalidFix("not a JSON object")
    missing = [key for key in FIX_FIELDS if key not in data]
    if missing:
        raise InvalidFix(f"no {', '.join(missing)}")
    mode = data["mode"]
    if mode not in ("2d", "3d"):
        raise InvalidFix(f"no 2D or 3D fix: mode {str(mode)[:20]!r}")
    satellites = data["satellites"]
    if satellites is not None and (isinstance(satellites, bool) or not isinstance(satellites, int)
                                   or not 0 <= satellites <= SATELLITES_MAX):
        raise InvalidFix(f"satellites is not a count: {str(satellites)[:20]!r}")
    return Fix(latitude=_number(data, "lat", *LATITUDE, required=True),
               longitude=_number(data, "lon", *LONGITUDE, required=True),
               altitude=_number(data, "alt", *ALTITUDE_RANGE),
               speed=_number(data, "speed", 0, SPEED_MAX),
               heading=_number(data, "track", 0, 360),
               mode=mode, satellites=satellites,
               hdop=_number(data, "hdop", 0, HDOP_MAX),
               gps_time=_gps_time(data["time"]))


@dataclass(frozen=True)
class Place:
    """A latitude and longitude that passed ``check_place``: what a Fixed Position holds."""

    latitude: float
    longitude: float

    def public(self) -> dict[str, float]:
        return {"latitude": self.latitude, "longitude": self.longitude}


def check_place(latitude: Any, longitude: Any) -> Place:
    """A Fixed Position's latitude and longitude, checked with the rules a fix's pass: each a
    finite JSON number (never a bool or a string), latitude within ±90 and longitude within ±180.
    Raises ``InvalidFix`` saying which is wrong."""
    data = {"latitude": latitude, "longitude": longitude}
    return Place(latitude=_number(data, "latitude", *LATITUDE, required=True),
                 longitude=_number(data, "longitude", *LONGITUDE, required=True))


async def lines(chunks: AsyncIterator[bytes], limit: int = MAX_LINE) -> AsyncIterator[bytes]:
    """The newline-separated lines of a byte stream. A line is kept to ``limit + 1`` bytes, so an
    endless one costs no memory and still fails ``check_fix``'s length check. A last line with no
    newline (the stream broke) is dropped."""
    buffer = bytearray()
    async for chunk in chunks:
        start = 0
        while (end := chunk.find(b"\n", start)) >= 0:
            buffer += chunk[start:end][:max(0, limit + 1 - len(buffer))]
            yield bytes(buffer)
            buffer.clear()
            start = end + 1
        buffer += chunk[start:][:max(0, limit + 1 - len(buffer))]


# -- the service -----------------------------------------------------------------------------


ClientFor = Callable[[Device], DeviceClient]


class FixedStore(Protocol):
    """Where Fixed Positions persist (``position_store.PositionStore``)."""

    def load(self) -> dict[str, Place]: ...
    def save(self, fixed: dict[str, Place]) -> None: ...


def _identity(device: Device) -> tuple:
    """What the stream depends on: a change to any of it reopens the stream."""
    return device.address, device.username, device.password, device.fingerprint


@dataclass
class _Refusals:
    count: int = 0
    logged_at: float | None = None


@dataclass
class _Followed:
    """What the service holds of one Device with an API, created on first use and dropped with the
    Device in one place (``_follow_registry``)."""

    fix: Fix | None = None  # the last accepted fix
    arrived: float = 0.0  # when it arrived
    fix_interval: float = FAST_INTERVAL_SECONDS  # the stream interval it came at
    demand: float | None = None  # when its Position Demand was last renewed
    stale: bool = False  # stale as last published
    trail: deque[tuple[Fix, float]] | None = None  # accepted fixes, when each arrived
    connected: bool = False  # a Sidecar stream is open
    refusals: _Refusals = field(default_factory=_Refusals)


class PositionService:
    def __init__(self, registry: DeviceRegistry, timings: PositionTimings | None = None, *,
                 store: FixedStore | None = None, client_for: ClientFor = DeviceClient.for_device,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._registry = registry
        self._timings = timings or PositionTimings()
        self._store = store
        self._fixed: dict[str, Place] = store.load() if store is not None else {}  # label -> Fixed Position
        self._client_for = client_for
        self._clock = clock
        self._followed: dict[str, _Followed] = {}  # label -> what is held of that Device
        self._tasks: dict[str, tuple[tuple, asyncio.Task]] = {}
        self._running = False
        self._listeners: list[Callable[[], None]] = []
        self._sidecar_listeners: list[Callable[[], None]] = []
        self._fixed_listeners: list[Callable[[], None]] = []
        registry.subscribe(self._follow_registry)
        self._forget_fixed()  # a Device removed while the file was edited by hand, say

    # -- wiring ---------------------------------------------------------------------------

    def subscribe(self, listener: Callable[[], None]) -> None:
        """Call ``listener()`` whenever ``positions()`` changes."""
        self._listeners.append(listener)

    def on_sidecar_change(self, listener: Callable[[], None]) -> None:
        """Call ``listener()`` whenever a Device's ``gps_sidecar`` flips."""
        self._sidecar_listeners.append(listener)

    def on_fixed_change(self, listener: Callable[[], None]) -> None:
        """Call ``listener()`` whenever ``fixed_positions()`` changes."""
        self._fixed_listeners.append(listener)

    # -- Fixed Positions --------------------------------------------------------------------

    def fixed_positions(self) -> dict[str, dict[str, float]]:
        """Every Device label that has a Fixed Position, to it: the ``fixed_positions`` store key."""
        return {label: place.public() for label, place in self._fixed.items()}

    def set_fixed(self, label: str, place: Place) -> None:
        """Give the Device ``label`` (which the caller checked exists) this Fixed Position."""
        self._save_fixed({**self._fixed, label: place})

    def clear_fixed(self, label: str) -> None:
        """Clear the Device's Fixed Position, if it has one."""
        if label in self._fixed:
            self._save_fixed({key: place for key, place in self._fixed.items() if key != label})

    def _save_fixed(self, fixed: dict[str, Place]) -> None:
        """Persist, then change and announce: a store that fails raises before anything changed."""
        if self._store is not None:
            self._store.save(fixed)
        self._fixed = fixed
        for listener in self._fixed_listeners:
            listener()
        self._announce()

    def _forget_fixed(self) -> None:
        """Drop, from the store too, the Fixed Position of every Device no longer registered."""
        present = {device.label for device in self._registry.devices()}
        if not self._fixed.keys() <= present:
            self._save_fixed({label: place for label, place in self._fixed.items() if label in present})

    # -- what it shows ----------------------------------------------------------------------

    def positions(self) -> dict[str, dict[str, Any]]:
        """Every Device label that has a Position, to that Position: the ``positions`` store key.
        A fresh fix first, then the Fixed Position, then the last fix marked stale. ``age`` is
        seconds since the fix arrived, at the moment this is called; a Fixed Position has none."""
        now = self._clock()
        shown = {}
        for label in self._fixes().keys() | self._fixed.keys():
            followed = self._followed.get(label)
            fix = followed.fix if followed is not None else None
            fresh = fix is not None and now - followed.arrived < self._stale_after(label, now)
            if not fresh and label in self._fixed:
                shown[label] = _fixed_view(self._fixed[label])
            else:
                shown[label] = self._view(fix, now - followed.arrived, self._stale_after(label, now))
        return shown

    def _fixes(self) -> dict[str, _Followed]:
        """Every followed Device that has had an accepted fix."""
        return {label: followed for label, followed in self._followed.items() if followed.fix is not None}

    def _of(self, label: str) -> _Followed:
        followed = self._followed.get(label)
        if followed is None:
            followed = self._followed[label] = _Followed()
        return followed

    def trail(self, label: str) -> dict[str, Any]:
        """The Device's Trail: ``seconds`` it reaches back, and its ``fixes`` oldest first, each
        ``{latitude, longitude, gps_time, age}`` with ``age`` in seconds since the fix arrived."""
        now = self._clock()
        followed = self._followed.get(label)
        trail = followed.trail if followed is not None else None
        if trail is not None:
            self._trim(trail, now)
        return {"seconds": self._timings.trail_seconds,
                "fixes": [{"latitude": fix.latitude, "longitude": fix.longitude, "gps_time": fix.gps_time,
                           "age": round(max(now - arrived, 0.0), 1)} for fix, arrived in trail or ()]}

    def _trim(self, trail: deque[tuple[Fix, float]], now: float) -> None:
        while trail and now - trail[0][1] > self._timings.trail_seconds:
            trail.popleft()

    def view(self, label: str) -> dict[str, Any]:
        """The fields this service adds to a Device's API view."""
        followed = self._followed.get(label)
        return {"gps_sidecar": followed is not None and followed.connected}

    def _view(self, fix: Fix, age: float, stale_after: float) -> dict[str, Any]:
        return {"source": "gps", "latitude": fix.latitude, "longitude": fix.longitude,
                "altitude": fix.altitude, "speed": fix.speed, "heading": fix.heading, "fix": fix.mode,
                "satellites": fix.satellites, "hdop": fix.hdop, "gps_time": fix.gps_time,
                "age": round(max(age, 0.0), 1), "stale": age >= stale_after}

    def _stale_after(self, label: str, now: float) -> float:
        """How old ``label``'s last fix may get before its Position is stale: ``stale_seconds``,
        plus the idle interval if the fix came at it or the stream runs at it now."""
        followed = self._followed.get(label)
        fix_interval = followed.fix_interval if followed is not None else FAST_INTERVAL_SECONDS
        slowest = max(fix_interval, self._interval(label, now))
        return self._timings.stale_seconds + (slowest if slowest > FAST_INTERVAL_SECONDS else 0.0)

    # -- Position Demand --------------------------------------------------------------------

    def renew(self, label: str) -> bool:
        """Renew ``label``'s Position Demand; False if there is no such Device. A renewal that
        brings the fast rate reopens the stream at once."""
        device = self._registry.get(label)
        if device is None:
            return False
        if device.api:
            now = self._clock()
            was = self._interval(label, now)
            self._of(label).demand = now
            if self._interval(label, now) != was:
                self._follow_registry()
        return True

    def _interval(self, label: str, now: float) -> float:
        """The stream interval ``label`` should run at: one second while it has live Position
        Demand, the idle interval otherwise."""
        followed = self._followed.get(label)
        renewed = followed.demand if followed is not None else None
        if renewed is not None and now - renewed < self._timings.demand_linger_seconds:
            return FAST_INTERVAL_SECONDS
        return self._timings.idle_seconds

    def _check_demand(self) -> None:
        """Forget Position Demand whose linger has run out, and slow those streams down."""
        now = self._clock()
        lapsed = [followed for followed in self._followed.values()
                  if followed.demand is not None and now - followed.demand >= self._timings.demand_linger_seconds]
        for followed in lapsed:
            followed.demand = None
        if lapsed:
            self._follow_registry()

    # -- running ----------------------------------------------------------------------------

    async def run(self) -> None:
        """Follow every Device's Sidecar and watch for Positions going stale, until cancelled."""
        self._running = True
        self._follow_registry()
        try:
            while True:
                await asyncio.sleep(TICK_SECONDS)
                self._check_demand()
                self._check_staleness()
        finally:
            self._running = False
            for _, task in self._tasks.values():
                task.cancel()
            self._tasks.clear()

    def _follow_registry(self) -> None:
        """Start a stream for each new (or changed) Device with an API, or one whose rate changed;
        end and forget those gone, Fixed Positions included (any Device may have one, with an API
        or not)."""
        self._forget_fixed()
        devices = {device.label: device for device in self._registry.devices() if device.api}
        changed = False
        for label in list(self._tasks.keys() - devices.keys()):
            self._tasks.pop(label)[1].cancel()
        for label in self._followed.keys() - devices.keys():
            changed |= self._followed.pop(label).fix is not None
        if self._running:
            now = self._clock()
            for label, device in devices.items():
                interval = self._interval(label, now)
                key = (_identity(device), interval)
                current = self._tasks.get(label)
                if current is not None and current[0] == key:
                    continue
                if current is not None:
                    current[1].cancel()
                task = asyncio.get_running_loop().create_task(self._stream(device, interval),
                                                              name=f"position-{label}")
                self._tasks[label] = (key, task)
        if changed:
            self._announce()

    def _current(self, label: str) -> bool:
        """Whether the running task is still the one following ``label``: a cancelled one must not
        touch state after its Device was removed or re-pinned."""
        entry = self._tasks.get(label)
        return entry is not None and entry[1] is asyncio.current_task()

    async def _stream(self, device: Device, interval: float) -> None:
        label = device.label
        client = self._client_for(device)
        path = f"/v1/fixes?interval={interval:g}"
        # Seconds before the next try once a Sidecar has answered; None until then. A stream
        # reopened at a new rate (or after a re-pin) starts from a Sidecar already known.
        known = self._followed.get(label)
        backoff = BACKOFF_FIRST_SECONDS if known is not None and known.connected else None
        while True:
            opened = False
            try:
                async with client.stream(GPS_SIDECAR_PORT, path,
                                         read_timeout=interval + READ_SLACK_SECONDS) as chunks:
                    opened = True
                    backoff = backoff or BACKOFF_FIRST_SECONDS
                    self._set_connected(label, True)
                    async for line in lines(chunks):
                        if not self._current(label):
                            return
                        if self._receive(label, line, interval):
                            backoff = BACKOFF_FIRST_SECONDS  # a stream that delivers starts the backoff over
            except DeviceError as exc:
                if not opened:
                    log.debug("Device %s: no GPS Sidecar: %s", label, exc)
            except asyncio.CancelledError:
                raise
            except Exception:  # never let one bad stream end the following
                log.exception("Device %s: the GPS Sidecar stream failed", label)
            if not self._current(label):
                return
            if not opened:
                self._set_connected(label, False)
            if backoff is None:
                await asyncio.sleep(self._timings.probe_seconds)
            else:
                await asyncio.sleep(min(backoff, self._timings.probe_seconds))
                backoff = None if backoff >= self._timings.probe_seconds else backoff * 2

    def _receive(self, label: str, line: bytes, interval: float) -> bool:
        """Take one line from ``label``'s Sidecar; whether it was an accepted fix."""
        try:
            fix = check_fix(line)
        except InvalidFix as exc:
            self._refuse(label, exc)
            return False
        now = self._clock()
        followed = self._of(label)
        followed.fix, followed.arrived, followed.fix_interval = fix, now, interval
        if followed.trail is None:
            limit = math.ceil(self._timings.trail_seconds * TRAIL_FIXES_PER_SECOND) + 1
            followed.trail = deque(maxlen=limit)
        followed.trail.append((fix, now))
        self._trim(followed.trail, now)
        followed.stale = False
        self._announce()
        return True

    def _refuse(self, label: str, reason: InvalidFix) -> None:
        refusals = self._of(label).refusals
        refusals.count += 1
        now = self._clock()
        if refusals.logged_at is None or now - refusals.logged_at >= REFUSAL_LOG_SECONDS:
            log.warning("Device %s's GPS Sidecar sent a line the instance refused (%s); %d refused "
                        "since the last report", label, reason, refusals.count)
            refusals.logged_at, refusals.count = now, 0

    def _set_connected(self, label: str, connected: bool) -> None:
        followed = self._of(label)
        if followed.connected == connected:
            return
        followed.connected = connected
        log.info("Device %s: GPS Sidecar %s", label, "connected" if connected else "lost")
        for listener in self._sidecar_listeners:
            listener()

    def _check_staleness(self) -> None:
        now = self._clock()
        changed = False
        for label, followed in self._fixes().items():
            stale = now - followed.arrived >= self._stale_after(label, now)
            if stale != followed.stale:
                followed.stale = stale
                changed = True
        if changed:
            self._announce()

    def _announce(self) -> None:
        for listener in self._listeners:
            listener()


def _fixed_view(place: Place) -> dict[str, Any]:
    return {"source": "fixed", "latitude": place.latitude, "longitude": place.longitude,
            "altitude": None, "speed": None, "heading": None, "fix": None, "satellites": None,
            "hdop": None, "gps_time": None, "age": None, "stale": False}
