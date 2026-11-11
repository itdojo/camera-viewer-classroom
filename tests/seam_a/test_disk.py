"""Seam A: free disk space for the captures folder, and the thresholds that
protect it. Below 10% or 5 GB free the instance warns; below 1 GB it refuses
new Recordings and Snapshots; below 500 MB it stops and finalizes every
active Recording, so a full disk never leaves a corrupt file.

These tests never fill a disk. Each level is triggered by starting an
instance whose threshold for it is set above the real free space."""

import secrets
import shutil
import struct
import time
import zlib

import pytest

GB = 1_000_000_000
# Docker Desktop's file sharing reports a bind mount's free space rounded to
# a few GB, so the instance's reading and the host's agree only roughly.
SLACK = 10 * GB
PETABYTE = "1P"  # above the free space of any disk these tests run on


def host_usage(instance):
    return shutil.disk_usage(instance.captures_dir)


def agrees_with_host(reading, host):
    return reading["total"] == host.total and abs(reading["free"] - host.free) <= max(SLACK, host.total // 100)


@pytest.fixture(scope="module")
def low(instances):
    """``low(name, VAR=value, ...)`` starts an instance with those settings;
    every one is removed after the module."""
    started = []

    def start(name, **env):
        own = instances.start(f"{name}-{secrets.token_hex(3)}", env=env)
        started.append(own)
        return own

    yield start
    for own in started:
        instances.remove(own)


def start_recording(api, camera_id):
    return api.post("/api/recordings", json={"camera": camera_id})


def test_the_instance_reports_free_and_total_space_of_the_captures_filesystem(instance):
    with instance.client() as api:
        response = api.get("/api/disk")
    host = host_usage(instance)

    assert response.status_code == 200, response.text
    disk = response.json()
    assert agrees_with_host(disk, host), (disk, host)
    # The spec's defaults: warn below 10% or 5 GB, refuse below 1 GB.
    expected = ("ok" if host.free >= 5 * GB and host.free >= host.total / 10
                else "warn" if host.free >= GB else disk["level"])
    assert disk["level"] == expected


@pytest.mark.parametrize("env", [
    {"CV_DISK_WARN_BYTES": PETABYTE, "CV_DISK_WARN_PERCENT": "0"},
    {"CV_DISK_WARN_PERCENT": "100", "CV_DISK_WARN_BYTES": "0"},
], ids=["below-warn-bytes", "below-warn-percent"])
def test_below_either_warning_threshold_the_instance_warns_but_still_records(
        low, env, test_pattern_device, unique_label):
    own = low("warn", **env)
    label = unique_label("warn")
    own.add_device(test_pattern_device, label=label)

    with own.client() as api, own.events() as events:
        pushed = events.next("disk")
        disk = api.get("/api/disk").json()
        started = start_recording(api, f"{label}/test-pattern")
        stopped = api.post(f"/api/recordings/{started.json()['id']}/stop")
        api.delete(f"/api/devices/{label}")

    assert (disk["level"], pushed["level"]) == ("warn", "warn")
    assert disk["refusing"] is False
    assert started.status_code == 201, started.text
    assert stopped.status_code == 200, stopped.text


def tiny_png() -> bytes:
    """A valid 2x2 truecolour PNG."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + b"\x00\xa8\x8a" * 2 for _ in range(2))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def captures(instance):
    return sorted(p.name for p in instance.captures_dir.iterdir() if not p.name.startswith("."))


def test_below_the_refuse_threshold_new_recordings_and_snapshots_are_refused_with_a_reason(
        low, test_pattern_device, unique_label):
    own = low("refuse", CV_DISK_REFUSE_BYTES=PETABYTE)
    label = unique_label("refuse")
    own.add_device(test_pattern_device, label=label)
    camera = f"{label}/test-pattern"

    with own.client() as api:
        disk = api.get("/api/disk").json()
        recording = start_recording(api, camera)
        snapshot = api.post("/api/snapshots", params={"camera": camera}, content=tiny_png(),
                            headers={"Content-Type": "image/png"})
        unfinished = api.get("/api/recordings").json()
        api.delete(f"/api/devices/{label}")

    assert (disk["level"], disk["refusing"]) == ("refuse", True)
    for refused in (recording, snapshot):
        assert (refused.status_code, refused.json()["error"]) == (507, "low_disk"), refused.text
        assert "refused" in refused.json()["message"]
    assert unfinished == []
    assert captures(own) == []


CHECK_SECONDS = 8


def wait_for(api, recording_id, states, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = api.get(f"/api/recordings/{recording_id}").json()
        if current["state"] in states:
            return current
        time.sleep(0.2)
    raise AssertionError(f"recording {recording_id} still {current['state']} after {timeout} s")


def test_below_the_finalize_threshold_every_active_recording_is_stopped_into_a_playable_file(
        low, test_pattern_device, unique_label):
    # Only the cutoff is set above the real free space, so a Recording may
    # still start and the next measurement ends it.
    own = low("cutoff", CV_DISK_FINALIZE_BYTES=PETABYTE, CV_DISK_CHECK_SECONDS=str(CHECK_SECONDS))
    labels = [unique_label("cut") for _ in range(2)]
    for label in labels:
        own.add_device(test_pattern_device, label=label)
    first, second = (f"{label}/test-pattern" for label in labels)

    with own.client() as api:
        disk = api.get("/api/disk").json()
        # The first Recording ends at whichever measurement comes next. Started
        # just after that one, the second has a whole interval to get its file.
        early = wait_for(api, start_recording(api, first).json()["id"], {"finalized"}, CHECK_SECONDS + 20)
        rec = start_recording(api, second).json()
        active = wait_for(api, rec["id"], {"active", "finalized"}, CHECK_SECONDS)
        final = wait_for(api, rec["id"], {"finalized"}, CHECK_SECONDS + 20)
        for label in labels:
            api.delete(f"/api/devices/{label}")

    assert disk["level"] == "finalize"
    assert early["reason"] == "low_disk"
    assert active["state"] == "active", "the Recording was cut off before it had a file"
    assert final["reason"] == "low_disk"
    [name] = final["captures"]
    assert name in captures(own)
    probe = own.probe_capture(name)
    [video] = [s for s in probe["streams"] if s["codec_type"] == "video"]
    assert (video["width"], video["height"]) > (0, 0)
    assert float(probe["format"]["duration"]) > 1


def test_free_space_is_pushed_to_every_browser_as_it_is_measured(low):
    own = low("live", CV_DISK_CHECK_SECONDS="1")

    with own.events() as first, own.events() as second:
        opening = first.next("disk")
        readings = [first.next("disk", timeout=5), first.next("disk", timeout=5)]
        other = second.next("disk", where=lambda d: d["checked"] > opening["checked"], timeout=5)
    host = host_usage(own)

    checked = [opening["checked"]] + [r["checked"] for r in readings]
    assert checked == sorted(set(checked)), "each event is a new measurement"
    for reading in [*readings, other]:
        assert agrees_with_host(reading, host), (reading, host)
