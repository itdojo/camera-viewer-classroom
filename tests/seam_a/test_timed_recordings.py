"""Seam A: timed Recordings — besides "until stopped", a Recording can run for
one of the instance's preset durations and then finalize on its own. Timers
count wall-clock time from the click, so a Feed drop eats into the duration,
and a timed Recording can still be stopped early.

The presets are 30 s, 1 min and 5 min by default; these tests run their own
instance with presets of a few seconds (``CV_RECORDING_PRESETS``)."""

import secrets
import time

import pytest

PRESETS = (3, 5, 8)
# The test pattern's GOP is one second (30 frames at 30 fps): the Relay closes
# a file on a keyframe, so a timer lands within about one GOP of its preset.
# The rest is the hook round trip and this test's polling.
GOP = 1.0
SLACK = 1.5


@pytest.fixture(scope="module")
def timed(instances):
    own = instances.start(f"timed-{secrets.token_hex(3)}",
                          env={"CV_RECORDING_PRESETS": ",".join(map(str, PRESETS))})
    yield own
    instances.remove(own)


def add_camera(instance, device, label):
    instance.add_device(device, label=label)
    return f"{label}/test-pattern"


def start(api, camera_id, **body):
    response = api.post("/api/recordings", json={"camera": camera_id, **body})
    assert response.status_code == 201, response.text
    return response.json()


def recording(api, recording_id):
    response = api.get(f"/api/recordings/{recording_id}")
    assert response.status_code == 200, response.text
    return response.json()


def wait_until_finalized(api, recording_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = recording(api, recording_id)
        if current["state"] == "finalized":
            return current
        time.sleep(0.1)
    raise AssertionError(f"recording {recording_id} still {current['state']} after {timeout} s")


def duration_of(instance, file_name):
    return float(instance.probe_capture(file_name)["format"]["duration"])


def test_the_instance_offers_its_configured_presets(timed):
    with timed.client() as api:
        response = api.get("/api/recordings/presets")

    assert response.status_code == 200
    assert response.json() == {"presets": list(PRESETS)}


def test_each_preset_finalizes_on_its_own_when_its_time_is_up(timed, test_pattern_device, unique_label):
    cameras = {seconds: add_camera(timed, test_pattern_device, unique_label(f"t{seconds}"))
               for seconds in PRESETS}

    with timed.client() as api:
        started = {}
        for seconds, camera in cameras.items():
            started[seconds] = (start(api, camera, duration=seconds), time.monotonic())
        ended = {}
        for seconds, (rec, at) in started.items():
            assert rec["duration"] == seconds
            ended[seconds] = (wait_until_finalized(api, rec["id"], seconds + 10), time.monotonic() - at)
        for camera in cameras.values():
            api.delete(f"/api/devices/{camera.split('/')[0]}")

    for seconds, (final, elapsed) in ended.items():
        assert final["reason"] == "timer"
        assert seconds - 0.2 <= elapsed <= seconds + GOP + SLACK, (seconds, elapsed)
        [file_name] = final["captures"]
        # The file starts when the on-demand Feed arrives, a moment after the click.
        assert seconds - 2.5 <= duration_of(timed, file_name) <= seconds + GOP, file_name


def test_a_timed_recording_stopped_early_finalizes_at_once(timed, test_pattern_device, unique_label):
    camera = add_camera(timed, test_pattern_device, unique_label("early"))
    longest = PRESETS[-1]

    with timed.client() as api:
        rec = start(api, camera, duration=longest)
        time.sleep(3)
        clicked = time.monotonic()
        response = api.post(f"/api/recordings/{rec['id']}/stop")
        answered = time.monotonic() - clicked
        time.sleep(longest - 3 + SLACK)  # past the timer: it must not fire on a stopped Recording
        later = recording(api, rec["id"])
        api.delete(f"/api/devices/{camera.split('/')[0]}")

    assert response.status_code == 200, response.text
    stopped = response.json()
    assert (stopped["state"], stopped["reason"]) == ("finalized", "stopped")
    assert answered <= GOP + SLACK
    [file_name] = stopped["captures"]
    assert duration_of(timed, file_name) <= 3 + GOP
    assert (later["reason"], later["captures"]) == ("stopped", stopped["captures"])


def test_a_timed_recording_reports_how_long_it_has_left(timed, test_pattern_device, unique_label):
    camera = add_camera(timed, test_pattern_device, unique_label("left"))
    longest = PRESETS[-1]

    with timed.client() as api, timed.events() as events:
        rec = start(api, camera, duration=longest)
        time.sleep(2)
        listed = next(r for r in api.get("/api/recordings").json() if r["id"] == rec["id"])
        pushed = events.next("recordings", where=lambda rs: any(r["id"] == rec["id"] for r in rs))
        api.post(f"/api/recordings/{rec['id']}/stop")
        api.delete(f"/api/devices/{camera.split('/')[0]}")

    assert rec["duration"] == longest
    assert longest - 1 <= rec["remaining"] <= longest
    assert longest - 3 <= listed["remaining"] <= longest - 1.5
    assert next(r for r in pushed if r["id"] == rec["id"])["duration"] == longest


def test_until_stopped_is_still_the_default_and_other_durations_are_refused(
        timed, test_pattern_device, unique_label):
    camera = add_camera(timed, test_pattern_device, unique_label("dflt"))

    with timed.client() as api:
        odd = api.post("/api/recordings", json={"camera": camera, "duration": 4})
        rec = start(api, camera)
        api.post(f"/api/recordings/{rec['id']}/stop")
        api.delete(f"/api/devices/{camera.split('/')[0]}")

    assert (odd.status_code, odd.json()["error"]) == (422, "unknown_preset")
    assert odd.json()["presets"] == list(PRESETS)
    assert (rec["duration"], rec["remaining"]) == (None, None)


def test_the_timer_counts_wall_clock_time_so_a_feed_drop_eats_into_it(
        timed, device_harness, unique_label):
    device = device_harness.add_device(unique_label("drop-dev"))
    camera = add_camera(timed, device, unique_label("drop"))
    longest = PRESETS[-1]
    try:
        with timed.client() as api:
            rec = start(api, camera, duration=longest)
            at = time.monotonic()
            time.sleep(3)
            device.stop()  # the Feed drops for good: nothing more reaches the file
            final = wait_until_finalized(api, rec["id"], longest + 10)
            elapsed = time.monotonic() - at
            api.delete(f"/api/devices/{camera.split('/')[0]}")
    finally:
        device_harness.remove_device(device)

    assert final["reason"] == "timer"
    assert longest - 0.2 <= elapsed <= longest + GOP + SLACK
    assert final["captures"]
    assert all(duration_of(timed, name) <= 3 + GOP for name in final["captures"])
