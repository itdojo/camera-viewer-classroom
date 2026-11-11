"""Seam A: a Device whose clock is more than an hour off the instance's is Down "Device clock wrong".

RTSPS keys its encryption with a timestamped message (MIKEY), and MediaMTX
refuses one more than an hour off its own clock, so such a Device adds fine
and then never delivers a frame, while the Relay sees only a 400. The
instance reads the Device's clock from the ``Date`` header of its API
answers, when the Device is added and every few seconds after.

The Devices here are scripted, because a container cannot have a clock of
its own: containers share the kernel's, and MediaMTX is a static Go binary
that libfaketime cannot reach (docs/working-notes.md, Ruled out). So these
tests prove the detection, not the 400 itself.
"""

import time

import pytest

DOWN_THRESHOLD = 5  # seconds: the instance's default Down threshold (ADR 0008)
RECOVERS_WITHIN = 30  # seconds: the instance re-reads every Device's clock every few seconds
REASON = "Device clock wrong"
HOUR = 3600


def pattern_path():
    return {"name": "test-pattern", "available": True, "ready": True,
            "tracks2": [{"codec": "H264", "codecProps": {"width": 1280, "height": 720}}]}


@pytest.fixture
def scripted_device(instance, instance_image, device_harness, unique_label):
    """A scripted Device with one Camera and its clock ``offset`` seconds off, added to the
    shared instance; removed from both afterwards."""
    made = []

    def make(prefix, offset):
        label = unique_label(prefix)
        device = device_harness.add_scripted_device(label, instance_image, paths=[pattern_path()],
                                                    clock_offset=offset)
        made.append(device)
        return device, instance.add_device(device, label=label)

    yield make
    for device in made:
        with instance.client() as api:
            api.delete(f"/api/devices/{device.label}")
        device_harness.remove_scripted_device(device)


def camera_view(api, label):
    return next(c for c in api.get("/api/cameras").json() if c["device"] == label)


def device_view(api, label):
    return next(d for d in api.get("/api/devices").json() if d["label"] == label)


def wait_for_camera(instance, label, condition, timeout):
    deadline = time.monotonic() + timeout
    with instance.client() as api:
        while True:
            camera = camera_view(api, label)
            if condition(camera):
                return camera
            if time.monotonic() > deadline:
                raise AssertionError(f"{label}'s Camera never got there; last seen {camera}")
            time.sleep(0.25)


@pytest.mark.parametrize("offset", [-(3 * HOUR + 35 * 60), 2 * HOUR], ids=["behind", "ahead"])
def test_a_device_with_a_wrong_clock_is_down_device_clock_wrong(instance, scripted_device, offset):
    device, created = scripted_device("skewed", offset)

    assert created["clock_wrong"] is True
    assert abs(created["clock_skew_seconds"] - offset) <= 5
    camera = wait_for_camera(instance, device.label, lambda c: c["feed_status"] == "down", DOWN_THRESHOLD)
    assert camera["feed_status_reason"] == REASON
    with instance.client() as api:
        assert device_view(api, device.label)["clock_wrong"] is True


def test_a_clock_within_the_hour_is_not_flagged(instance, scripted_device):
    device, created = scripted_device("near", 40 * 60)

    assert created["clock_wrong"] is False
    assert abs(created["clock_skew_seconds"] - 40 * 60) <= 5
    time.sleep(DOWN_THRESHOLD + 1)
    with instance.client() as api:
        assert camera_view(api, device.label)["feed_status_reason"] != REASON


def test_a_device_whose_clock_is_fixed_stops_being_flagged(instance, scripted_device):
    device, created = scripted_device("fixed", -2 * HOUR)
    assert created["clock_wrong"] is True
    wait_for_camera(instance, device.label, lambda c: c["feed_status_reason"] == REASON, DOWN_THRESHOLD)

    device.set_clock_offset(0)  # NTP came back

    wait_for_camera(instance, device.label, lambda c: c["feed_status_reason"] != REASON, RECOVERS_WITHIN)
    with instance.client() as api:
        view = device_view(api, device.label)
        assert view["clock_wrong"] is False
        assert abs(view["clock_skew_seconds"]) <= 5


def test_a_device_whose_clock_drifts_after_it_was_added_goes_down(instance, scripted_device):
    device, created = scripted_device("drifted", 0)
    assert created["clock_wrong"] is False

    device.set_clock_offset(-2 * HOUR)

    camera = wait_for_camera(instance, device.label, lambda c: c["feed_status_reason"] == REASON, RECOVERS_WITHIN)
    assert camera["feed_status"] == "down"


def test_a_real_device_s_clock_is_read_and_not_flagged(instance, test_pattern_device, unique_label):
    label = unique_label("on-time")

    created = instance.add_device(test_pattern_device, label=label)

    assert created["clock_wrong"] is False
    assert abs(created["clock_skew_seconds"]) <= 5  # MediaMTX's API answers carry a Date header
