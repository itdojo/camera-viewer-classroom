"""Seam A: Position Demand sets the rate of a Device's GPS Sidecar stream (#64, ADR 0012).

A Device streams one fix per idle interval until someone renews its Position Demand with
``PUT /api/devices/<label>/position-demand``, then one a second until the linger after the last
renewal runs out. What is observed is the gap between accepted fixes, as ``positions`` events
carry them. The instance is this module's own, with short ``POSITION_*`` timings, so the idle
interval and the linger take seconds rather than the defaults' 30 and 25.
"""

import itertools
import time

import pytest

from harness.gps import Epoch, track_lines, write_track

IDLE_SECONDS = 4
LINGER_SECONDS = 4
STALE_SECONDS = 2  # shorter than the idle interval on purpose: an idle Position must not read stale
TRACK = "still.nmea"
EPOCH = Epoch("120000.00", 48.8566, 2.3522, 35.0, 0.0, 0.0)

FAST = 1.6  # the longest gap between fixes at 1 Hz, with the Sidecar's and the network's slack
IDLE = IDLE_SECONDS - 0.6  # the shortest gap between fixes at the idle interval


@pytest.fixture(scope="module")
def demand_instance(instances):
    return instances.start("position-demand", env={"POSITION_IDLE_SECONDS": str(IDLE_SECONDS),
                                                   "POSITION_DEMAND_LINGER_SECONDS": str(LINGER_SECONDS),
                                                   "POSITION_STALE_SECONDS": str(STALE_SECONDS)})


@pytest.fixture(autouse=True)
def demand_instance_logs_on_failure(request):
    """As ``conftest.instance_logs_on_failure``, for this module's own instance."""
    yield
    if getattr(request.node, "seam_a_failed", False) and "demand_instance" in request.fixturenames:
        logs = request.getfixturevalue("demand_instance").logs()
        print(f"\n--- position-demand instance logs (tail) ---\n{logs[-8000:]}")


@pytest.fixture
def gps_device(demand_instance, device_harness, unique_label, tmp_path):
    """A test-pattern Device whose Sidecar replays one fix a second, added to the instance."""
    tracks = write_track(tmp_path / "tracks", track_lines([EPOCH]), TRACK)
    device = device_harness.add_device(unique_label("demand"), gps=True, gps_tracks=tracks,
                                       settings={"GPS_SOURCE": f"replay:{TRACK}"})
    demand_instance.add_device(device)
    yield device
    with demand_instance.client() as api:
        api.delete(f"/api/devices/{device.label}")


def renew(instance, label):
    with instance.client() as api:
        return api.put(f"/api/devices/{label}/position-demand")


def watch(stream, label, seconds, *, every=None):
    """Follow ``label``'s Position for ``seconds``: the arrival times of its accepted fixes and
    every Position published meanwhile. A fix just accepted is published with an age of 0; the
    same Position re-sent for another Device's fix, or on going stale, is older. ``every``, if
    given, is a callable run about once a second (a renewal)."""
    fixes, published = [], []
    deadline = time.monotonic() + seconds
    next_call = time.monotonic()
    while (now := time.monotonic()) < deadline:
        if every is not None and now >= next_call:
            every()
            next_call = now + 1
        wait = min(deadline, next_call) - now if every is not None else deadline - now
        try:
            position = stream.next("positions", lambda p: label in p, timeout=max(wait, 0.05))[label]
        except TimeoutError:
            continue
        published.append(position)
        if position["age"] < 0.5:
            fixes.append(time.monotonic())
    return fixes, published


def gaps(times):
    return [round(later - earlier, 2) for earlier, later in itertools.pairwise(times)]


def test_an_unwatched_device_streams_at_the_idle_interval_and_its_position_stays_fresh(
        demand_instance, gps_device):
    with demand_instance.events() as stream:
        stream.next("positions", lambda p: gps_device.label in p and p[gps_device.label]["age"] < 0.5, timeout=15)
        fixes, published = watch(stream, gps_device.label, 3 * IDLE_SECONDS + 1)

    assert len(fixes) >= 2, gaps(fixes)
    assert all(gap >= IDLE for gap in gaps(fixes)), gaps(fixes)
    assert not any(position["stale"] for position in published), published


def test_renewing_position_demand_brings_1_hz_and_the_idle_interval_returns_after_the_linger(
        demand_instance, gps_device):
    with demand_instance.events() as stream:
        stream.next("positions", lambda p: gps_device.label in p and p[gps_device.label]["age"] < 0.5, timeout=15)
        assert renew(demand_instance, gps_device.label).status_code == 204
        renewed = time.monotonic()
        fixes, published = watch(stream, gps_device.label, LINGER_SECONDS + 3 * IDLE_SECONDS + 1)

    fast = [t for t in fixes if t < renewed + LINGER_SECONDS]
    after = [t for t in fixes if t > renewed + LINGER_SECONDS + 1.5]
    assert fast and fast[0] - renewed < 2.5, (fast, renewed)  # within a second or two (story 42)
    assert len(fast) >= LINGER_SECONDS - 1 and all(gap <= FAST for gap in gaps(fast)), gaps(fast)
    assert len(after) >= 3 and all(gap >= IDLE for gap in gaps(after[1:])), gaps(after)
    assert not any(position["stale"] for position in published), published


def test_renewals_keep_the_device_at_1_hz_past_the_linger(demand_instance, gps_device):
    with demand_instance.events() as stream:
        stream.next("positions", lambda p: gps_device.label in p and p[gps_device.label]["age"] < 0.5, timeout=15)
        renew(demand_instance, gps_device.label)
        time.sleep(2)
        fixes, _ = watch(stream, gps_device.label, 2 * LINGER_SECONDS + 2,
                         every=lambda: renew(demand_instance, gps_device.label))

    assert len(fixes) >= 2 * LINGER_SECONDS, gaps(fixes)
    assert all(gap <= FAST for gap in gaps(fixes)), gaps(fixes)


def test_position_demand_for_an_unknown_device_is_refused(demand_instance, unique_label):
    response = renew(demand_instance, unique_label("nobody"))
    assert response.status_code == 404
    assert response.json()["error"] == "device_not_found"


def test_position_demand_is_refused_without_a_session(demand_instance, gps_device):
    with demand_instance.client(login=False) as api:
        assert api.put(f"/api/devices/{gps_device.label}/position-demand").status_code == 401
