"""Seam A: a Device's Trail, its accepted fixes of the last ``POSITION_TRAIL_SECONDS``, in memory (#60).

``GET /api/devices/<label>/trail`` answers ``{"seconds": <trail length>, "fixes": [...]}``, oldest
first, each fix ``{latitude, longitude, gps_time, age}`` with ``age`` in seconds since it arrived.
The Devices run the kit's real Sidecar replaying a track this module writes, so every fix is a
known literal. They join an instance of this module's own whose Trail is a few seconds long, so
the length cap shows without waiting the default ten minutes.
"""

import itertools
import time

import pytest

from harness.gps import Epoch, track_lines, write_track

TRAIL_SECONDS = 5
STALE_SECONDS = 3
PROBE_SECONDS = 4
TRACK = "trail.nmea"

# Due north-east, one epoch a second: the stream's 1 Hz.
EPOCHS = [
    Epoch("100000.00", 51.5, -0.125, 35.0, 5.0, 12.5),
    Epoch("100001.00", 51.5001, -0.1249, 36.0, 5.0, 12.5),
    Epoch("100002.00", 51.5002, -0.1248, 37.0, 5.0, 12.5),
    Epoch("100003.00", 51.5003, -0.1247, 38.0, 5.0, 12.5),
]
WHERE = {epoch.gps_time: (epoch.lat, epoch.lon) for epoch in EPOCHS}
ORDER = [epoch.gps_time for epoch in EPOCHS]
HOME = {"latitude": 51.47793, "longitude": -0.00148}


@pytest.fixture(scope="module")
def trail_instance(instances):
    return instances.start("trail", env={"POSITION_TRAIL_SECONDS": str(TRAIL_SECONDS),
                                         "POSITION_STALE_SECONDS": str(STALE_SECONDS),
                                         "POSITION_PROBE_SECONDS": str(PROBE_SECONDS),
                                         # every stream at 1 Hz with no Position Demand (#64)
                                         "POSITION_IDLE_SECONDS": "1"})


@pytest.fixture(autouse=True)
def trail_instance_logs_on_failure(request):
    """As ``conftest.instance_logs_on_failure``, for this module's own instance."""
    yield
    if getattr(request.node, "seam_a_failed", False) and "trail_instance" in request.fixturenames:
        logs = request.getfixturevalue("trail_instance").logs()
        print(f"\n--- trail instance logs (tail) ---\n{logs[-8000:]}")


@pytest.fixture(scope="module")
def tracks(tmp_path_factory):
    return write_track(tmp_path_factory.mktemp("tracks"), track_lines(EPOCHS), TRACK)


@pytest.fixture
def gps_device(trail_instance, device_harness, unique_label, tracks):
    """A Device whose Sidecar replays ``EPOCHS``, added to the instance and removed afterwards."""
    device = device_harness.add_device(unique_label("trail"), gps=True, gps_tracks=tracks,
                                       settings={"GPS_SOURCE": f"replay:{TRACK}"})
    trail_instance.add_device(device)
    yield device
    with trail_instance.client() as api:
        api.delete(f"/api/devices/{device.label}")


def get_trail(instance, label, **kwargs):
    with instance.client(**kwargs) as api:
        return api.get(f"/api/devices/{label}/trail")


def trail_of(instance, label):
    response = get_trail(instance, label)
    assert response.status_code == 200, response.text
    return response.json()


def wait_for_fixes(instance, label, count, timeout=20):
    """The Trail once it holds at least ``count`` fixes."""
    deadline = time.monotonic() + timeout
    while len((trail := trail_of(instance, label))["fixes"]) < count:
        assert time.monotonic() < deadline, trail
        time.sleep(0.5)
    return trail


def test_the_trail_holds_the_accepted_fixes_oldest_first(trail_instance, gps_device):
    trail = wait_for_fixes(trail_instance, gps_device.label, 4)

    fixes = trail["fixes"]
    for fix in fixes:
        assert set(fix) == {"latitude", "longitude", "gps_time", "age"}
        assert (fix["latitude"], fix["longitude"]) == pytest.approx(WHERE[fix["gps_time"]], abs=1e-6)
    ages = [fix["age"] for fix in fixes]
    assert ages == sorted(ages, reverse=True), ages  # oldest first
    times = [fix["gps_time"] for fix in fixes]
    for earlier, later in itertools.pairwise(times):  # forward along the loop; a 1 Hz stream may skip one
        assert (ORDER.index(later) - ORDER.index(earlier)) % len(ORDER) in (1, 2), times


def test_the_trail_reaches_back_only_trail_seconds_and_empties_once_the_sidecar_stops(
        trail_instance, gps_device):
    wait_for_fixes(trail_instance, gps_device.label, 1)
    time.sleep(TRAIL_SECONDS + 3)  # long enough for the first fixes to have aged out

    trail = trail_of(trail_instance, gps_device.label)

    assert trail["seconds"] == TRAIL_SECONDS
    ages = [fix["age"] for fix in trail["fixes"]]
    assert all(age <= TRAIL_SECONDS for age in ages), ages
    assert TRAIL_SECONDS - 1 <= len(ages) <= TRAIL_SECONDS + 1, ages  # one fix a second

    gps_device.stop_gps()
    time.sleep(TRAIL_SECONDS + 1)

    assert trail_of(trail_instance, gps_device.label)["fixes"] == []


def test_removing_a_device_discards_its_trail(trail_instance, gps_device):
    label = gps_device.label
    wait_for_fixes(trail_instance, label, 2)
    gps_device.stop_gps()  # so nothing new can arrive once it is back

    with trail_instance.client() as api:
        assert api.delete(f"/api/devices/{label}").status_code == 204
    assert get_trail(trail_instance, label).status_code == 404
    trail_instance.add_device(gps_device)  # the same label, with fixes still well inside the Trail's reach

    assert trail_of(trail_instance, label)["fixes"] == []


def test_a_fixed_position_never_enters_the_trail(trail_instance, test_pattern_device, unique_label):
    label = unique_label("trail-fixed")
    trail_instance.add_device(test_pattern_device, label=label)
    try:
        with trail_instance.client() as api:
            assert api.put(f"/api/devices/{label}/fixed-position", json=HOME).status_code == 200
        with trail_instance.events() as stream:
            assert stream.next("positions", lambda positions: label in positions)[label]["source"] == "fixed"

        assert trail_of(trail_instance, label) == {"seconds": TRAIL_SECONDS, "fixes": []}
    finally:
        with trail_instance.client() as api:
            api.delete(f"/api/devices/{label}")


def test_a_device_that_does_not_exist_has_no_trail(trail_instance):
    response = get_trail(trail_instance, "no-such-device")

    assert response.status_code == 404
    assert response.json()["error"] == "device_not_found"


def test_the_trail_is_refused_without_a_session(trail_instance, gps_device):
    wait_for_fixes(trail_instance, gps_device.label, 1)

    response = get_trail(trail_instance, gps_device.label, login=False)

    assert response.status_code == 401
    assert "fixes" not in response.text
