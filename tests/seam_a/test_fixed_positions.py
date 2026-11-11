"""Seam A: a Fixed Position, entered by hand for a Device, is its Position until a fresh fix beats it (#58).

``PUT /api/devices/<label>/fixed-position`` with ``{"latitude", "longitude"}`` sets or changes it and
``DELETE`` clears it. ``positions`` shows it with ``source: "fixed"`` whenever the Device has no fresh
fix, and ``fixed_positions`` carries every Device's Fixed Position for the Devices Panel. The tests run
on an instance of this module's own, with a short staleness threshold, because they also restart it.
"""

import time

import pytest

from harness.gps import Epoch, track_lines, write_track

STALE_SECONDS = 3
PROBE_SECONDS = 4
TRACK = "still.nmea"
EPOCH = Epoch("093005.00", 51.50001, -0.12346, 35.4, 5.0, 47.0, satellites=9, hdop=0.8)
# A mounted Camera on the far side of town.
HOME = {"latitude": 51.47793, "longitude": -0.00148}
MOVED = {"latitude": -33.85681, "longitude": 151.21514}


def fixed(where):
    """The Position the instance must publish for a Fixed Position at ``where``."""
    return {"source": "fixed", **where, "altitude": None, "speed": None, "heading": None, "fix": None,
            "satellites": None, "hdop": None, "gps_time": None, "age": None, "stale": False}


@pytest.fixture(scope="module")
def fixed_instance(instances):
    # An idle interval of 1 s keeps every stream at 1 Hz with no Position Demand (#64).
    return instances.start("fixed", env={"POSITION_STALE_SECONDS": str(STALE_SECONDS),
                                         "POSITION_PROBE_SECONDS": str(PROBE_SECONDS),
                                         "POSITION_IDLE_SECONDS": "1"})


@pytest.fixture(autouse=True)
def fixed_instance_logs_on_failure(request):
    """As ``conftest.instance_logs_on_failure``, for this module's own instance."""
    yield
    if getattr(request.node, "seam_a_failed", False) and "fixed_instance" in request.fixturenames:
        logs = request.getfixturevalue("fixed_instance").logs()
        print(f"\n--- fixed instance logs (tail) ---\n{logs[-8000:]}")


@pytest.fixture
def plain_device(fixed_instance, test_pattern_device, unique_label):
    """The shared test-pattern Device (no GPS Sidecar) under a label of this test's own."""
    label = unique_label("fixed")
    fixed_instance.add_device(test_pattern_device, label=label)
    yield label
    with fixed_instance.client() as api:
        api.delete(f"/api/devices/{label}")


@pytest.fixture
def gps_device(fixed_instance, device_harness, unique_label, tmp_path):
    """A Device whose Sidecar replays ``EPOCH`` every second, added to the instance."""
    tracks = write_track(tmp_path / "tracks", track_lines([EPOCH]), TRACK)
    device = device_harness.add_device(unique_label("fixed-gps"), gps=True, gps_tracks=tracks,
                                       settings={"GPS_SOURCE": f"replay:{TRACK}"})
    fixed_instance.add_device(device)
    yield device
    with fixed_instance.client() as api:
        api.delete(f"/api/devices/{device.label}")


def put(instance, label, body, **kwargs):
    with instance.client(**kwargs) as api:
        return api.put(f"/api/devices/{label}/fixed-position", json=body)


def delete(instance, label, **kwargs):
    with instance.client(**kwargs) as api:
        return api.delete(f"/api/devices/{label}/fixed-position")


def has(label, source=None):
    return lambda positions: label in positions and source in (None, positions[label]["source"])


def test_a_fixed_position_becomes_the_position_of_a_device_without_gps(fixed_instance, plain_device):
    with fixed_instance.events() as stream:
        response = put(fixed_instance, plain_device, HOME)
        assert response.status_code == 200, response.text
        assert response.json() == HOME

        assert stream.next("positions", has(plain_device))[plain_device] == fixed(HOME)


def test_the_fixed_positions_key_carries_it_for_the_device_panel(fixed_instance, plain_device):
    with fixed_instance.events() as stream:
        assert put(fixed_instance, plain_device, HOME).status_code == 200

        assert stream.next("fixed_positions", lambda fixed: plain_device in fixed)[plain_device] == HOME


def test_a_fixed_position_can_be_changed_and_cleared(fixed_instance, plain_device):
    assert put(fixed_instance, plain_device, HOME).status_code == 200

    with fixed_instance.events() as stream, fixed_instance.events() as panel:
        assert put(fixed_instance, plain_device, MOVED).status_code == 200
        stream.next("positions", lambda positions: positions.get(plain_device) == fixed(MOVED))
        panel.next("fixed_positions", lambda fixed: fixed.get(plain_device) == MOVED)

        assert delete(fixed_instance, plain_device).status_code == 204
        stream.next("positions", lambda positions: plain_device not in positions)
        panel.next("fixed_positions", lambda fixed: plain_device not in fixed)

    assert delete(fixed_instance, plain_device).status_code == 204  # clearing nothing is no error


@pytest.mark.parametrize(("body", "message"), [
    ({"latitude": 91, "longitude": 0}, "latitude is out of range"),
    ({"latitude": -90.5, "longitude": 0}, "latitude is out of range"),
    ({"latitude": 0, "longitude": 180.01}, "longitude is out of range"),
    ({"latitude": 0, "longitude": -181}, "longitude is out of range"),
    ({"latitude": "51.5", "longitude": 0}, "latitude is not a number"),
    ({"latitude": True, "longitude": 0}, "latitude is not a number"),
    ({"latitude": 0}, "longitude is not a number"),
    ({"latitude": None, "longitude": 0}, "latitude is not a number"),
])
def test_an_impossible_fixed_position_is_refused_and_changes_nothing(fixed_instance, plain_device, body, message):
    assert put(fixed_instance, plain_device, HOME).status_code == 200

    response = put(fixed_instance, plain_device, body)

    assert response.status_code == 422, response.text
    assert response.json()["error"] == "invalid_position"
    assert message in response.json()["message"]
    with fixed_instance.events() as stream:
        assert stream.next("positions", has(plain_device))[plain_device] == fixed(HOME)


def test_a_non_finite_fixed_position_is_refused(fixed_instance, plain_device):
    with fixed_instance.client() as api:
        response = api.put(f"/api/devices/{plain_device}/fixed-position",
                           content=b'{"latitude": NaN, "longitude": 0}', headers={"Content-Type": "application/json"})

    assert response.status_code == 422, response.text
    assert response.json()["error"] == "invalid_position"


def test_a_device_that_does_not_exist_has_no_fixed_position(fixed_instance):
    assert put(fixed_instance, "no-such-device", HOME).status_code == 404
    assert put(fixed_instance, "no-such-device", HOME).json()["error"] == "device_not_found"
    assert delete(fixed_instance, "no-such-device").status_code == 404


def test_a_fresh_fix_beats_the_fixed_position_and_a_stale_one_falls_back_to_it(fixed_instance, gps_device):
    label = gps_device.label
    with fixed_instance.events() as stream:
        stream.next("positions", has(label, "gps"), timeout=20)
        assert put(fixed_instance, label, HOME).status_code == 200

        # Fixes keep arriving, so the Position stays the live one.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            assert stream.next("positions", has(label))[label]["source"] == "gps"

        gps_device.stop_gps()
        assert stream.next("positions", has(label, "fixed"), timeout=STALE_SECONDS + 3)[label] == fixed(HOME)

        gps_device.start_gps()
        live = stream.next("positions", has(label, "gps"), timeout=PROBE_SECONDS + 5)[label]
        assert live["stale"] is False
        assert live["latitude"] == pytest.approx(EPOCH.lat, abs=1e-6)


def test_without_a_fixed_position_a_stale_fix_stays_shown_as_stale(fixed_instance, gps_device):
    label = gps_device.label
    with fixed_instance.events() as stream:
        stream.next("positions", has(label, "gps"), timeout=20)
        gps_device.stop_gps()
        stale = stream.next("positions", lambda positions: positions.get(label, {}).get("stale"),
                            timeout=STALE_SECONDS + 3)[label]

        assert stale["source"] == "gps"
        assert put(fixed_instance, label, HOME).status_code == 200
        assert stream.next("positions", has(label, "fixed"))[label] == fixed(HOME)
        assert delete(fixed_instance, label).status_code == 204
        back = stream.next("positions", has(label, "gps"))[label]
        assert back["stale"] is True
        assert back["latitude"] == pytest.approx(EPOCH.lat, abs=1e-6)


def test_removing_a_device_deletes_its_fixed_position(fixed_instance, test_pattern_device, unique_label):
    label = unique_label("fixed-rm")
    fixed_instance.add_device(test_pattern_device, label=label)
    assert put(fixed_instance, label, HOME).status_code == 200

    with fixed_instance.events() as stream, fixed_instance.events() as panel:
        with fixed_instance.client() as api:
            assert api.delete(f"/api/devices/{label}").status_code == 204
        stream.next("positions", lambda positions: label not in positions)
        panel.next("fixed_positions", lambda fixed: label not in fixed)

    # The same label, added again, starts with no Fixed Position, even after a restart.
    fixed_instance.add_device(test_pattern_device, label=label)
    fixed_instance.restart()
    with fixed_instance.events() as stream:
        assert label not in stream.next("positions")
        assert label not in stream.next("fixed_positions")
    with fixed_instance.client() as api:
        api.delete(f"/api/devices/{label}")


def test_a_fixed_position_survives_a_restart_in_a_store_only_the_instance_can_read(fixed_instance, plain_device):
    assert put(fixed_instance, plain_device, HOME).status_code == 200

    fixed_instance.restart()

    with fixed_instance.events() as stream:
        assert stream.next("positions", has(plain_device))[plain_device] == fixed(HOME)
    result = fixed_instance.exec("stat", "-c", "%a", "/config/positions.json")
    assert result.stdout.strip() == "600", result
    assert "latitude" not in fixed_instance.exec("cat", "/config/devices.json").stdout  # off the Device record


def test_fixed_position_routes_refuse_an_unauthenticated_request(fixed_instance, plain_device):
    assert put(fixed_instance, plain_device, HOME, login=False).status_code == 401
    assert delete(fixed_instance, plain_device, login=False).status_code == 401
    with fixed_instance.events() as stream:
        assert plain_device not in stream.next("positions")
