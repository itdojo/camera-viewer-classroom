"""Seam A: each Device's Position, streamed from its GPS Sidecar, reaches browsers as ``positions``.

The Devices here run the kit's real Sidecar in replay mode (``tests/harness/device-gps.yaml``),
looping a track this module writes, so every fix the instance may publish is a known literal.
They join an instance of this module's own whose ``POSITION_*`` timings are short, so staleness
and a late Sidecar show within seconds rather than the defaults' 10 and 30. Its idle interval is
the 1 Hz of Position Demand, so every stream here runs at one fix a second with no renewals; the
switch between rates is ``test_position_demand.py``'s.
"""

import itertools
import time

import pytest

from harness.gps import Epoch, track_lines, write_track

STALE_SECONDS = 3
PROBE_SECONDS = 4
TRACK = "track.nmea"

# Due north-east at 5 m/s (18 km/h), one epoch a second: the stream's 1 Hz.
EPOCHS = [
    Epoch("090000.00", 51.5, -0.125, 35.0, 5.0, 12.5, satellites=9, hdop=0.8),
    Epoch("090001.00", 51.5001, -0.1249, 36.0, 5.0, 12.5, satellites=10, hdop=0.9),
    Epoch("090002.00", 51.5002, -0.1248, 37.0, 5.0, 12.5, satellites=11, hdop=1.0),
    Epoch("090003.00", 51.5003, -0.1247, 38.0, 5.0, 12.5, satellites=12, hdop=1.1),
]


def expected(epoch):
    """The Position the instance must publish for an accepted fix of ``epoch``."""
    return {"source": "gps", "latitude": pytest.approx(epoch.lat, abs=1e-6),
            "longitude": pytest.approx(epoch.lon, abs=1e-6), "altitude": epoch.alt,
            "speed": pytest.approx(epoch.speed, abs=1e-3), "heading": epoch.track, "fix": "3d",
            "satellites": epoch.satellites, "hdop": epoch.hdop, "gps_time": epoch.gps_time}


EXPECTED = {epoch.gps_time: expected(epoch) for epoch in EPOCHS}
ORDER = [epoch.gps_time for epoch in EPOCHS]


@pytest.fixture(scope="module")
def gps_instance(instances):
    return instances.start("positions", env={"POSITION_STALE_SECONDS": str(STALE_SECONDS),
                                             "POSITION_PROBE_SECONDS": str(PROBE_SECONDS),
                                             "POSITION_IDLE_SECONDS": "1"})


@pytest.fixture(autouse=True)
def gps_instance_logs_on_failure(request):
    """As ``conftest.instance_logs_on_failure``, for this module's own instance."""
    yield
    if getattr(request.node, "seam_a_failed", False) and "gps_instance" in request.fixturenames:
        logs = request.getfixturevalue("gps_instance").logs()
        print(f"\n--- positions instance logs (tail) ---\n{logs[-8000:]}")


@pytest.fixture(scope="module")
def tracks(tmp_path_factory):
    return write_track(tmp_path_factory.mktemp("tracks"), track_lines(EPOCHS), TRACK)


@pytest.fixture
def gps_device(gps_instance, device_harness, unique_label, tracks):
    """Start a test-pattern Device whose Sidecar replays ``EPOCHS``. Afterwards it leaves the
    instance (if a test added it there) and ``no_leaked_devices`` stops it."""
    made = []

    def make(prefix="gps"):
        device = device_harness.add_device(unique_label(prefix), gps=True, gps_tracks=tracks,
                                           settings={"GPS_SOURCE": f"replay:{TRACK}"})
        made.append(device)
        return device

    yield make
    with gps_instance.client() as api:
        for device in made:
            api.delete(f"/api/devices/{device.label}")


def has(label):
    return lambda positions: label in positions


def positions_of(stream, label, count, timeout=20):
    """The next ``count`` Positions of ``label`` that ``positions`` events carry."""
    return [stream.next("positions", has(label), timeout=timeout)[label] for _ in range(count)]


def test_positions_follow_the_replayed_track(gps_instance, gps_device):
    device = gps_device()

    with gps_instance.events() as stream:
        gps_instance.add_device(device)
        seen = positions_of(stream, device.label, 6)

    for position in seen:
        assert {k: v for k, v in position.items() if k not in ("age", "stale")} == EXPECTED[position["gps_time"]]
        assert position["stale"] is False
        assert 0 <= position["age"] < STALE_SECONDS
    times = [gps_time for gps_time, _ in itertools.groupby(p["gps_time"] for p in seen)]
    assert len(times) >= 3, times
    for earlier, later in itertools.pairwise(times):  # forward along the loop; a 1 Hz stream may skip one
        assert (ORDER.index(later) - ORDER.index(earlier)) % len(ORDER) in (1, 2), times


def device_of(devices, label):
    """``label``'s entry in a Device list, or an empty one before it is listed."""
    return next((device for device in devices if device["label"] == label), {})


def test_the_devices_view_says_whether_a_gps_sidecar_is_connected_and_republishes_only_on_a_change(
        gps_instance, gps_device):
    device = gps_device()

    with gps_instance.events() as stream:
        gps_instance.add_device(device)
        stream.next("devices", lambda devices: device_of(devices, device.label).get("gps_sidecar") is True)
        stream.next("positions", has(device.label))
        stream.next("positions", has(device.label))
        with pytest.raises(TimeoutError):  # fixes keep coming, but the Device's view is unchanged
            stream.next("devices", timeout=3)


def test_a_device_without_a_gps_sidecar_has_no_position(gps_instance, gps_device):
    device = gps_device()
    device.stop_gps()

    with gps_instance.events() as stream:
        gps_instance.add_device(device)
        with pytest.raises(TimeoutError):
            stream.next("positions", has(device.label), timeout=PROBE_SECONDS)
    with gps_instance.client() as api:
        assert device_of(api.get("/api/devices").json(), device.label)["gps_sidecar"] is False


def test_a_position_goes_stale_with_its_age_once_the_sidecar_stops(gps_instance, gps_device):
    device = gps_device()

    # Two streams, since a stream drops the events it skips (working notes, Gotchas).
    with gps_instance.events() as stream, gps_instance.events() as device_events:
        gps_instance.add_device(device)
        last = positions_of(stream, device.label, 2)[-1]
        device.stop_gps()
        stopped_at = time.monotonic()
        while not (latest := stream.next("positions", has(device.label), timeout=STALE_SECONDS + 3)[device.label])["stale"]:
            last = latest  # a fix still in flight when the Sidecar stopped
        stale_after = time.monotonic() - stopped_at
        device_events.next("devices", lambda devices: device_of(devices, device.label).get("gps_sidecar") is False,
                           timeout=PROBE_SECONDS + 3)

    assert STALE_SECONDS <= latest["age"] < STALE_SECONDS + 1
    assert stale_after < STALE_SECONDS + 2
    keep = ("latitude", "longitude", "altitude", "gps_time")
    assert {k: latest[k] for k in keep} == {k: last[k] for k in keep}


def test_a_sidecar_started_after_the_device_was_added_is_picked_up_within_one_probe_interval(
        gps_instance, gps_device):
    device = gps_device("late")
    device.stop_gps()
    gps_instance.add_device(device)
    time.sleep(1)  # the instance tried the Sidecar when the Device was added, and found none

    with gps_instance.events() as stream:
        device.start_gps()  # returns once the Sidecar answers a handshake
        ready = time.monotonic()
        stream.next("positions", has(device.label), timeout=PROBE_SECONDS + 5)

    # One probe interval, plus the second a fix takes to fall due.
    assert time.monotonic() - ready < PROBE_SECONDS + 2


def test_removing_a_device_drops_its_position_and_stops_its_stream(gps_instance, gps_device):
    device = gps_device()

    with gps_instance.events() as stream:
        gps_instance.add_device(device)
        stream.next("positions", has(device.label))
        with gps_instance.client() as api:
            assert api.delete(f"/api/devices/{device.label}").status_code == 204
        stream.next("positions", lambda positions: device.label not in positions)
        with pytest.raises(TimeoutError):  # a stream still open would bring it back within a second
            stream.next("positions", has(device.label), timeout=3)


def test_positions_are_refused_without_a_session(gps_instance):
    with gps_instance.events(expect=None, login=False) as stream:
        assert stream.status_code == 401
        with pytest.raises(TimeoutError):
            stream.next("positions", timeout=1)
