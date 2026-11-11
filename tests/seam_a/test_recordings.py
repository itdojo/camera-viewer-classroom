"""Seam A: Recordings — start and stop on the instance, one playable MP4 per
Recording in the captures folder, named for its Device, Camera, local start
time and received resolution."""

import re
import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

NAME = re.compile(r"^(?P<stem>.+)-(?P<date>\d{8})-(?P<time>\d{6})-(?P<width>\d+)x(?P<height>\d+)\.mp4$")


def captures_of(instance, label):
    return sorted(p.name for p in instance.captures_dir.iterdir()
                  if p.is_file() and p.name.startswith(f"{label}-"))


def start(api, camera_id, **body):
    response = api.post("/api/recordings", json={"camera": camera_id, **body})
    assert response.status_code == 201, response.text
    return response.json()


def stop(api, recording):
    response = api.post(f"/api/recordings/{recording['id']}/stop")
    assert response.status_code == 200, response.text
    return response.json()


def test_a_started_then_stopped_recording_is_one_playable_mp4_named_for_its_feed(
        instance, test_pattern_device, unique_label):
    label = unique_label("rec")
    instance.add_device(test_pattern_device, label=label)
    camera = f"{label}/test-pattern"

    with instance.client() as api:
        before = datetime.now(UTC)
        recording = start(api, camera)
        assert recording["camera"] == camera
        assert recording["state"] in ("waiting", "active")
        # The clock starts once the Relay is writing, as in the quick-stop test below; from
        # start() alone, a slow Feed start left 2.4 s on CI (PR #74).
        assert wait_until(lambda: staged(instance, camera), timeout=15), "the Relay never opened a file"
        time.sleep(4)
        stopped = stop(api, recording)
        after = datetime.now(UTC)

    assert stopped["state"] == "finalized"
    assert stopped["reason"] == "stopped"
    files = captures_of(instance, label)
    assert files == stopped["captures"]
    assert len(files) == 1
    match = NAME.match(files[0])
    assert match, files[0]
    assert match["stem"] == f"{label}-test-pattern"
    assert (match["width"], match["height"]) == ("1280", "720")
    named = datetime.strptime(match["date"] + match["time"], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    assert before - timedelta(seconds=1) <= named <= after  # the shared instance runs in UTC

    probe = instance.probe_capture(files[0])
    video = [s for s in probe["streams"] if s["codec_type"] == "video"]
    assert [(s["codec_name"], s["width"], s["height"]) for s in video] == [("h264", 1280, 720)]
    assert [s for s in probe["streams"] if s["codec_type"] != "video"] == []
    assert 2.5 <= float(probe["format"]["duration"]) <= 8


def named_time(file_name):
    match = NAME.match(file_name)
    assert match, file_name
    return datetime.strptime(match["date"] + match["time"], "%Y%m%d%H%M%S")


def test_capture_names_carry_the_instance_local_time(instances, test_pattern_device, unique_label):
    kathmandu = timezone(timedelta(hours=5, minutes=45))  # an offset no developer's clock is in by accident
    own = instances.start(unique_label("tz"), env={"TZ": "Asia/Kathmandu"})
    label = unique_label("tz")
    own.add_device(test_pattern_device, label=label)

    with own.client() as api:
        before = datetime.now(kathmandu).replace(tzinfo=None, microsecond=0)
        recording = start(api, f"{label}/test-pattern")
        time.sleep(3)
        stopped = stop(api, recording)
        after = datetime.now(kathmandu).replace(tzinfo=None)

    [file_name] = stopped["captures"]
    assert before <= named_time(file_name) <= after


def test_two_cameras_record_at_the_same_time_into_separate_files(
        instance, test_pattern_device, unique_label):
    first, second = unique_label("left"), unique_label("right")
    instance.add_device(test_pattern_device, label=first)
    instance.add_device(test_pattern_device, label=second)

    with instance.client() as api:
        a = start(api, f"{first}/test-pattern")
        b = start(api, f"{second}/test-pattern")

        def both_active():
            listed = {r["id"]: r["state"] for r in api.get("/api/recordings").json()}
            return listed.get(a["id"]) == listed.get(b["id"]) == "active"

        # A file begins only once the Feed is pulled and its first keyframe
        # arrives, so time the overlap from when both are writing.
        assert wait_until(both_active, timeout=20, interval=0.1)
        time.sleep(4)
        assert both_active()
        stopped_a, stopped_b = stop(api, a), stop(api, b)
        remaining = {r["id"] for r in api.get("/api/recordings").json()}

    assert not {a["id"], b["id"]} & remaining
    assert captures_of(instance, first) == stopped_a["captures"]
    assert captures_of(instance, second) == stopped_b["captures"]
    for label, stopped in ((first, stopped_a), (second, stopped_b)):
        [file_name] = stopped["captures"]
        assert file_name.startswith(f"{label}-test-pattern-")
        duration = float(instance.probe_capture(file_name)["format"]["duration"])
        assert duration >= 3  # both ran through the whole overlap, not one after the other


def staged(instance, camera_id):
    folder = instance.captures_dir / ".recording" / camera_id
    return [p for p in folder.iterdir() if p.is_file()] if folder.is_dir() else []


def wait_until(predicate, timeout, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def started_second(recording):
    return datetime.fromisoformat(recording["started"]).astimezone(UTC).replace(tzinfo=None)


def test_a_recording_stopped_as_the_relay_opens_its_file_keeps_that_file_and_the_next_gets_its_own(
        instance, test_pattern_device, unique_label):
    """Stop lands while the Relay is already writing but before its "segment
    created" hook has reached the backend. The file is still this Recording's,
    and a Recording started straight after gets only files of its own."""
    label = unique_label("quick")
    instance.add_device(test_pattern_device, label=label)
    camera = f"{label}/test-pattern"

    with instance.client() as api:
        first = start(api, camera)
        assert wait_until(lambda: staged(instance, camera), timeout=15), "the Relay never opened a file"
        stopped_first = stop(api, first)
        second = start(api, camera)
        # The clock starts once the Relay is writing the second file: a file holds whole 1 s GOPs,
        # so 3 s from start() alone could come out under 2 s.
        assert wait_until(lambda: staged(instance, camera), timeout=15), "the Relay never opened a second file"
        time.sleep(3)
        stopped_second = stop(api, second)

    assert stopped_first["state"] == stopped_second["state"] == "finalized"
    assert len(stopped_first["captures"]) == 1, stopped_first
    assert len(stopped_second["captures"]) == 1, stopped_second
    assert captures_of(instance, label) == sorted(stopped_first["captures"] + stopped_second["captures"])
    [first_file], [second_file] = stopped_first["captures"], stopped_second["captures"]
    assert named_time(first_file) == started_second(first)
    assert named_time(second_file) == started_second(second)
    assert float(instance.probe_capture(second_file)["format"]["duration"]) >= 2
    assert staged(instance, camera) == []


@pytest.mark.parametrize("ui", [{"certificate": True}, {"tls": False}], ids=["supplied-certificate", "tls-off"])
def test_a_recording_produces_its_capture_however_the_ui_is_served(
        instances, test_pattern_device, unique_label, ui):
    """The Relay reports segments to the backend's own UI port. The shared
    instance covers the certificate it makes itself; these cover a supplied
    one, which the hook must trust instead, and plain HTTP."""
    other = instances.start(unique_label("ui-rec"), **ui)
    label = unique_label("ui-rec")
    other.add_device(test_pattern_device, label=label)

    with other.client() as api:
        recording = start(api, f"{label}/test-pattern")
        time.sleep(4)
        stopped = stop(api, recording)

    assert stopped["state"] == "finalized"
    [file_name] = stopped["captures"]
    assert captures_of(other, label) == [file_name]
    assert float(other.probe_capture(file_name)["format"]["duration"]) >= 2
    assert staged(other, f"{label}/test-pattern") == []


def camera_recording(cameras, camera_id):
    return next(c for c in cameras if c["id"] == camera_id)["recording"]


def test_every_browser_hears_a_recording_start_and_stop_on_its_camera(
        instance, test_pattern_device, unique_label):
    label = unique_label("badge")
    instance.add_device(test_pattern_device, label=label)
    camera = f"{label}/test-pattern"

    with instance.events() as events, instance.client() as api:
        recording = start(api, camera)
        listed = events.next("recordings", where=lambda rs: any(r["id"] == recording["id"] for r in rs))
        on_camera = events.next("cameras", where=lambda cs: camera_recording(cs, camera) is not None)
        active = events.next("cameras", where=lambda cs: (camera_recording(cs, camera) or {}).get("state") == "active")
        assert camera_recording(api.get("/api/cameras").json(), camera)["id"] == recording["id"]
        stop(api, recording)
        gone = events.next("recordings", where=lambda rs: all(r["id"] != recording["id"] for r in rs))
        cleared = events.next("cameras", where=lambda cs: camera_recording(cs, camera) is None)

    assert next(r for r in listed if r["id"] == recording["id"])["camera"] == camera
    assert camera_recording(on_camera, camera)["id"] == recording["id"]
    assert camera_recording(active, camera)["id"] == recording["id"]
    assert all(r["id"] != recording["id"] for r in gone)
    assert camera_recording(cleared, camera) is None


def test_a_camera_already_recording_refuses_a_second_recording(instance, test_pattern_device, unique_label):
    label = unique_label("twice")
    instance.add_device(test_pattern_device, label=label)
    camera = f"{label}/test-pattern"

    with instance.client() as api:
        first = start(api, camera)
        again = api.post("/api/recordings", json={"camera": camera})
        stop(api, first)

    assert again.status_code == 409
    assert again.json()["error"] == "already_recording"
    assert again.json()["recording"]["id"] == first["id"]


def test_only_the_relay_can_report_a_recording_segment(instance):
    # The Relay's segment hooks reach the backend without a session, so the
    # hook endpoint takes nothing but its own token: not a missing one, not a
    # wrong one, and not a browser session either.
    event = {"event": "complete", "path": "nobody/nothing", "segment": "/etc/passwd"}
    with instance.client(login=False) as anonymous, instance.client() as logged_in:
        responses = [
            anonymous.post("/internal/recorder/segments", json=event),
            anonymous.post("/internal/recorder/segments", json=event,
                           headers={"X-Camera-Viewer-Hook": "guess"}),
            logged_in.post("/internal/recorder/segments", json=event),
        ]

    assert [r.status_code for r in responses] == [403, 403, 403]
    assert {r.json()["error"] for r in responses} == {"forbidden"}


def test_recording_an_unknown_camera_or_stopping_an_unknown_recording_is_not_found(instance):
    with instance.client() as api:
        no_camera = api.post("/api/recordings", json={"camera": "nobody/nothing"})
        no_recording = api.post("/api/recordings/0123456789ab/stop")

    assert (no_camera.status_code, no_camera.json()["error"]) == (404, "camera_not_found")
    assert (no_recording.status_code, no_recording.json()["error"]) == (404, "recording_not_found")
