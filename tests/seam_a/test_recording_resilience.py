"""Seam A: Recordings that survive what happens around them — a Feed that
drops and comes back, a Device removed mid-Recording, an instance restarted
mid-Recording."""

import contextlib
import re
import time

import pytest

PART = re.compile(r"^(?P<base>.+-\d{8}-\d{6})-(?P<size>\d+x\d+)(?:-part-(?P<part>\d+))?\.mp4$")


def start(api, camera_id):
    response = api.post("/api/recordings", json={"camera": camera_id})
    assert response.status_code == 201, response.text
    return response.json()


def stop(api, recording):
    response = api.post(f"/api/recordings/{recording['id']}/stop")
    assert response.status_code == 200, response.text
    return response.json()


def recording_on(cameras, camera_id):
    return next(c for c in cameras if c["id"] == camera_id)["recording"]


def state_on(camera_id, state):
    return lambda cameras: (recording_on(cameras, camera_id) or {}).get("state") == state


def captures_of(instance, label):
    return sorted(p.name for p in instance.captures_dir.iterdir()
                  if p.is_file() and p.name.startswith(f"{label}-"))


def playable_seconds(instance, file_name):
    probe = instance.probe_capture(file_name)
    assert [s["codec_name"] for s in probe["streams"] if s["codec_type"] == "video"] == ["h264"], probe
    return float(probe["format"]["duration"])


@pytest.fixture
def own_device(device_harness, unique_label):
    """A Device this test may pause or remove; never the shared one."""
    device = device_harness.add_device(unique_label("drop"))
    yield device
    with contextlib.suppress(Exception):
        device.unpause()  # a test that failed mid-drop left it paused
    device_harness.remove_device(device)


def test_a_feed_that_drops_mid_recording_continues_into_a_part_2_file_with_the_same_base_name(
        instance, own_device, unique_label):
    label = unique_label("drop")
    camera = f"{label}/test-pattern"
    instance.add_device(own_device, label=label)

    with instance.events() as events, instance.client() as api:
        recording = start(api, camera)
        events.next("cameras", where=state_on(camera, "active"), timeout=20)
        time.sleep(3)
        own_device.pause()
        try:
            events.next("cameras", where=state_on(camera, "waiting"), timeout=40)
            [waiting] = [r for r in api.get("/api/recordings").json() if r["id"] == recording["id"]]
            assert waiting["state"] == "waiting"
        finally:
            own_device.unpause()
        events.next("cameras", where=state_on(camera, "active"), timeout=60)
        time.sleep(3)
        stopped = stop(api, recording)

    assert stopped["state"] == "finalized"
    files = captures_of(instance, label)
    assert files == sorted(stopped["captures"])
    assert len(files) == 2, files
    first, second = (PART.match(name) for name in stopped["captures"])
    assert first and second, stopped["captures"]
    assert first["part"] is None
    assert second["part"] == "2"
    assert first["base"] == second["base"]
    for name in stopped["captures"]:
        assert playable_seconds(instance, name) >= 1.5


def test_removing_a_device_finalizes_its_active_recording_into_a_playable_file(
        instance, test_pattern_device, unique_label):
    label = unique_label("gone")
    camera = f"{label}/test-pattern"
    instance.add_device(test_pattern_device, label=label)

    with instance.events() as events, instance.client() as api:
        recording = start(api, camera)
        events.next("cameras", where=state_on(camera, "active"), timeout=20)
        time.sleep(3)
        removing = time.monotonic()
        assert api.delete(f"/api/devices/{label}").status_code == 204
        removal_took = time.monotonic() - removing
        # Removal answers once the Recording is finalized: the Relay closed
        # the file while it still had the Device's path, well inside the
        # Recorder's 15 s fallback for a Relay that never reports.
        ended = api.get(f"/api/recordings/{recording['id']}")

    assert ended.status_code == 200, ended.text
    ended = ended.json()
    assert (ended["state"], ended["reason"]) == ("finalized", "device_removed")
    [file_name] = ended["captures"]
    assert captures_of(instance, label) == [file_name]
    assert playable_seconds(instance, file_name) >= 2
    assert removal_took < 10, removal_took


def staged_files(instance):
    staging = instance.captures_dir / ".recording"
    return [p for p in staging.rglob("*") if p.is_file()] if staging.exists() else []


def test_a_restart_mid_recording_leaves_a_playable_file_flagged_interrupted_that_never_resumes(
        instances, test_pattern_device, unique_label):
    own = instances.start(unique_label("restart"))
    label = unique_label("restart")
    camera = f"{label}/test-pattern"
    own.add_device(test_pattern_device, label=label)

    with own.events() as events, own.client() as api:
        recording = start(api, camera)
        events.next("cameras", where=state_on(camera, "active"), timeout=20)
        time.sleep(4)

    own.restart()

    with own.client() as api:
        after = api.get(f"/api/recordings/{recording['id']}")
        assert after.status_code == 200, after.text
        interrupted = after.json()
        assert interrupted["state"] == "interrupted"
        [file_name] = interrupted["captures"]
        assert captures_of(own, label) == [file_name]
        assert playable_seconds(own, file_name) >= 2

        stopping = time.monotonic()  # stopping it again is harmless and immediate
        again = api.post(f"/api/recordings/{recording['id']}/stop")
        assert time.monotonic() - stopping < 3
        assert (again.status_code, again.json()["state"]) == (200, "interrupted")

        time.sleep(6)  # long enough for the Relay to have opened a new segment, were it recording
        assert api.get("/api/recordings").json() == []
        assert recording_on(api.get("/api/cameras").json(), camera) is None
        assert staged_files(own) == []
        assert captures_of(own, label) == [file_name]

    own.restart()  # the flag is not a one-off: it outlives the next restart too

    with own.client() as api:
        again = api.get(f"/api/recordings/{recording['id']}").json()
    assert (again["state"], again["captures"]) == ("interrupted", [file_name])


def test_files_left_in_staging_by_a_lost_hook_become_captures_at_the_next_start(
        instances, test_pattern_device, unique_label):
    """A segment whose hooks never reached the backend (a failed hook, a
    crash between the Relay closing it and the backend placing it) has no
    Recording to claim it. The next start places it anyway, named from its
    staging folder, its own start time and the resolution inside the file."""
    own = instances.start(unique_label("orphans"))
    label = unique_label("orphan")
    camera = f"{label}/test-pattern"
    own.add_device(test_pattern_device, label=label)
    with own.client() as api:
        recording = start(api, camera)
        time.sleep(3)
        [made] = stop(api, recording)["captures"]
    # The Relay writes whole GOPs (1 s), so a 3 s Recording holds 2 or 3 s depending on where
    # the first keyframe falls; compare the placed file with its source, not with a guess.
    made_seconds = playable_seconds(own, made)

    own.stop()
    folder = own.captures_dir / ".recording" / label / "test-pattern"
    folder.mkdir(parents=True, exist_ok=True)
    orphan = folder / "20200101-120000-000000.mp4"
    orphan.write_bytes((own.captures_dir / made).read_bytes())
    own.start()

    expected = f"{label}-test-pattern-20200101-120000-1280x720.mp4"
    assert expected in captures_of(own, label)
    assert staged_files(own) == []
    assert playable_seconds(own, expected) == made_seconds
    with own.client() as api:
        assert expected in [c["name"] for c in api.get("/api/captures").json()]
