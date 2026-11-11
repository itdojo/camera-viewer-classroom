"""Seam A: a hostile GPS Sidecar puts nothing on screen, and an impostor never gets the credentials.

Each hostile Device here is the scripted Device API (``harness/scripted_device_api.py``) with its
scripted Sidecar on 9998, streaming lines this module chose, round after round, at about five
rounds a second. Beside them runs a well-behaved Device whose kit Sidecar replays a known track, so
a test can see the instance keep serving it while another Device sends garbage. The instance is
this module's own, so its log holds only these Devices, and its probe interval is short, so an
impostor is retried within seconds.
"""

import base64
import json
import time

import pytest

from harness.gps import Epoch, track_lines, write_track

PROBE_SECONDS = 3
OBSERVE_SECONDS = 10  # well under a minute, and hundreds of refused lines
TRACK = "track.nmea"
REFUSAL_LOG = "GPS Sidecar sent a line the instance refused"

EPOCHS = [
    Epoch("120000.00", 48.8566, 2.3522, 35.0, 2.0, 270.0),
    Epoch("120001.00", 48.8567, 2.3521, 35.5, 2.0, 270.0),
    Epoch("120002.00", 48.8568, 2.3520, 36.0, 2.0, 270.0),
]
REPLAYED = {epoch.gps_time: (epoch.lat, epoch.lon) for epoch in EPOCHS}


def fix(**fields):
    """A Sidecar fix line: every field the kit's Sidecar sends (#54), overridden by ``fields``.
    A field passed as ``...`` is left out."""
    base = {"time": "2026-10-15T09:00:00.000Z", "lat": 10.0, "lon": 20.0, "alt": 5.0, "speed": 1.0,
            "track": 90.0, "mode": "3d", "satellites": 7, "hdop": 1.2}
    return json.dumps({k: v for k, v in {**base, **fields}.items() if v is not ...}).encode()


# Every line a Sidecar might send that must never become a Position. Each fix-shaped one carries
# coordinates of its own, so one that leaked would show which.
HOSTILE = {
    "latitude above 90": fix(lat=90.5, lon=1.0),
    "latitude below -90": fix(lat=-91.0, lon=2.0),
    "longitude above 180": fix(lat=3.0, lon=180.01),
    "longitude below -180": fix(lat=4.0, lon=-500.0),
    "NaN latitude": fix(lat=float("nan"), lon=5.0),
    "infinite longitude": fix(lat=6.0, lon=float("inf")),
    "minus-infinite speed": fix(lat=7.0, lon=7.0, speed=float("-inf")),
    "a number that overflows to infinity": fix(lat=8.0, lon=8.0).replace(b'"alt": 5.0', b'"alt": 1e999'),
    "a 400-digit integer": fix(lat=10**400, lon=9.0),
    "negative speed": fix(lat=11.0, lon=11.0, speed=-0.1),
    "speed past the ceiling": fix(lat=12.0, lon=12.0, speed=301.0),
    "track past 360": fix(lat=13.0, lon=13.0, track=360.5),
    "no fix": fix(lat=14.0, lon=14.0, mode="none"),
    "an unknown fix mode": fix(lat=15.0, lon=15.0, mode="4d"),
    "a missing field": fix(lat=16.0, lon=16.0, hdop=...),
    "no latitude": fix(lat=..., lon=17.0),
    "a null latitude": fix(lat=None, lon=18.0),
    "latitude as text": fix(lat="19.0", lon=19.0),
    "latitude as a boolean": fix(lat=True, lon=20.0),
    "negative satellites": fix(lat=21.0, lon=21.0, satellites=-1),
    "a time that is not a time": fix(lat=22.0, lon=22.0, time="yesterday"),
    "a valid fix past 1 KB": fix(lat=23.0, lon=23.0) + b" " * 1100,
    "a 64 KB line": b'{"lat": 24.0, "pad": "' + b"x" * 65536 + b'"}',
    "raw NMEA": b"$GPRMC,090000.00,A,5130.0000,N,00007.5000,W,9.719,12.5,151026,,,A*6B",
    "binary": bytes(range(256)).replace(b"\n", b""),
    "an empty line": b"",
    "a JSON array": b"[25.0, 25.0]",
    "unclosed nesting": b"[" * 1000,
}
# A good fix, last in every round of the Device that sends one: seeing it proves the instance read
# every hostile line before it, and refused them.
MARKER = fix(lat=-33.85, lon=151.21, alt=58.0, speed=0.5, track=45.0, satellites=11, hdop=0.7)
MARKER_POSITION = {"source": "gps", "latitude": -33.85, "longitude": 151.21, "altitude": 58.0, "speed": 0.5,
                   "heading": 45.0, "fix": "3d", "satellites": 11, "hdop": 0.7,
                   "gps_time": "2026-10-15T09:00:00.000Z"}


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


@pytest.fixture(scope="module")
def gps_instance(instances):
    # An idle interval of 1 s keeps the honest Device's stream at 1 Hz with no Position Demand (#64).
    return instances.start("hostile-gps", env={"POSITION_PROBE_SECONDS": str(PROBE_SECONDS),
                                               "POSITION_IDLE_SECONDS": "1"})


@pytest.fixture(autouse=True)
def gps_instance_logs_on_failure(request):
    """As ``conftest.instance_logs_on_failure``, for this module's own instance."""
    yield
    if getattr(request.node, "seam_a_failed", False) and "gps_instance" in request.fixturenames:
        logs = request.getfixturevalue("gps_instance").logs()
        print(f"\n--- hostile-gps instance logs (tail) ---\n{logs[-8000:]}")


@pytest.fixture
def added(gps_instance):
    """Add Devices to the module's instance; each leaves it afterwards, and ``no_leaked_devices``
    stops its container."""
    labels = []

    def add(device):
        gps_instance.add_device(device)
        labels.append(device.label)
        return device

    yield add
    with gps_instance.client() as api:
        for label in labels:
            api.delete(f"/api/devices/{label}")


@pytest.fixture
def replay_device(device_harness, unique_label, tmp_path):
    """A well-behaved Device: the kit's Sidecar replaying ``EPOCHS``."""
    tracks = write_track(tmp_path / "tracks", track_lines(EPOCHS), TRACK)
    return device_harness.add_device(unique_label("good"), gps=True, gps_tracks=tracks,
                                     settings={"GPS_SOURCE": f"replay:{TRACK}"})


def hostile_device(device_harness, image, label, lines, **kwargs):
    return device_harness.add_scripted_device(
        label, image, paths=[{"name": "cam", "available": True}], sidecar_lines=lines, **kwargs)


def without_age(position):
    return {k: v for k, v in position.items() if k not in ("age", "stale")}


def test_no_hostile_line_ever_becomes_a_position_and_other_devices_are_served_meanwhile(
        gps_instance, added, device_harness, instance_image, unique_label, replay_device):
    marked = hostile_device(device_harness, instance_image, unique_label("hostile"), [*HOSTILE.values(), MARKER])
    garbage = hostile_device(device_harness, instance_image, unique_label("garbage"), list(HOSTILE.values()))
    seen = {marked.label: [], garbage.label: [], replay_device.label: []}

    with gps_instance.events() as stream:
        added(replay_device)
        added(marked)
        added(garbage)
        deadline = time.monotonic() + OBSERVE_SECONDS
        while (left := deadline - time.monotonic()) > 0:
            try:
                positions = stream.next("positions", timeout=left)
            except TimeoutError:
                break
            for label, history in seen.items():
                if label in positions:
                    history.append(positions[label])

    # The marked Device's stream was read through every round, and only its good fix was shown.
    assert seen[marked.label], "the marked Device's good fix never arrived"
    leaked = [p for p in seen[marked.label] if without_age(p) != MARKER_POSITION]
    assert leaked == [], f"hostile lines became Positions: {leaked[:3]}"
    assert seen[garbage.label] == []
    user, password = marked.credentials  # control: it was trusted, so it did get the credentials
    assert {r["authorization"] for r in marked.sidecar_requests()} == {basic(user, password)}

    # Meanwhile the well-behaved Device's Positions kept moving along its track.
    replayed = {p["gps_time"] for p in seen[replay_device.label]}
    assert len(replayed) >= 3, replayed
    for position in seen[replay_device.label]:
        assert (position["latitude"], position["longitude"]) == pytest.approx(REPLAYED[position["gps_time"]], abs=1e-6)
        assert position["stale"] is False

    # The instance is up, and still sees both hostile Sidecars as connected.
    with gps_instance.client() as api:
        devices = {d["label"]: d for d in api.get("/api/devices").json()}
    assert devices[marked.label]["gps_sidecar"] is True
    assert devices[garbage.label]["gps_sidecar"] is True
    assert devices[replay_device.label]["gps_sidecar"] is True


def test_refused_lines_are_logged_at_most_once_per_device_per_minute(
        gps_instance, added, device_harness, instance_image, unique_label):
    first = hostile_device(device_harness, instance_image, unique_label("noisy"), list(HOSTILE.values()))
    second = hostile_device(device_harness, instance_image, unique_label("noisy"), list(HOSTILE.values()))

    added(first)
    added(second)
    time.sleep(OBSERVE_SECONDS)  # about five rounds a second, so hundreds of refused lines each

    logs = gps_instance.logs()
    for device in (first, second):
        reports = [line for line in logs.splitlines() if f"Device {device.label}'s {REFUSAL_LOG}" in line]
        assert len(reports) == 1, reports
    assert first.sidecar_requests() and second.sidecar_requests()  # control: the instance read their streams


def test_a_sidecar_presenting_another_certificate_is_refused_before_any_credential(
        gps_instance, added, device_harness, instance_image, unique_label):
    """An impostor on the Device's Sidecar port, with a certificate of its own for the same label,
    sending a perfectly good fix: only the pin keeps it out."""
    impostor = hostile_device(device_harness, instance_image, unique_label("impostor"), [MARKER],
                              sidecar_impostor=True)
    assert impostor.sidecar_fingerprint != impostor.fingerprint

    with gps_instance.events() as stream:
        added(impostor)  # pinned to the Device API's certificate, which is the Device's own
        with pytest.raises(TimeoutError):
            stream.next("positions", lambda positions: impostor.label in positions, timeout=2 * PROBE_SECONDS + 1)

    assert impostor.sidecar_connections() >= 2  # tried at the add and again after a probe interval
    assert impostor.sidecar_requests() == []  # refused in the handshake: no request, so no credentials
    with gps_instance.client() as api:
        view = next(d for d in api.get("/api/devices").json() if d["label"] == impostor.label)
    assert view["gps_sidecar"] is False
