"""Seam A: the Captures list — every Recording and Snapshot in the captures
folder, newest first, with its size and kind, pushed to every browser as it
changes; each one can be downloaded byte for byte and deleted.

Download and delete take a Capture name from the client, so they must never
reach anything but a Capture: not a path outside the folder, not the store's
own staging areas, not a symlink, and never a file a Recording is still
writing or extending."""

from __future__ import annotations

import contextlib
import struct
import time
import zlib

import pytest


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(width: int, height: int) -> bytes:
    rows = b"".join(b"\x00" + bytes((0, 168, 132)) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


@pytest.fixture
def camera(instance, test_pattern_device, unique_label):
    """A Camera of its own on the shared instance, removed again afterwards."""
    label = unique_label("caps")
    instance.add_device(test_pattern_device, label=label)
    yield f"{label}/test-pattern"
    with instance.client() as api:
        api.delete(f"/api/devices/{label}")


def snapshot(api, camera_id, image: bytes) -> str:
    response = api.post("/api/snapshots", params={"camera": camera_id}, content=image,
                        headers={"Content-Type": "image/png"})
    assert response.status_code == 201, response.text
    return response.json()["capture"]


def named(captures, name):
    return next((c for c in captures if c["name"] == name), None)


def test_a_new_snapshot_reaches_every_open_event_stream_with_its_size_and_kind(instance, camera):
    image = png(32, 18)
    with instance.events() as events, instance.client() as api:
        name = snapshot(api, camera, image)
        captures = events.next("captures", where=lambda data: named(data, name) is not None)

    entry = named(captures, name)
    assert entry["kind"] == "snapshot"
    assert entry["size"] == len(image)
    assert entry["interrupted"] is False


def test_the_list_is_newest_first_and_skips_what_is_not_a_capture(instance, camera):
    with instance.client() as api:
        older = snapshot(api, camera, png(8, 8))
        time.sleep(0.05)
        newer = snapshot(api, camera, png(16, 16))
        (instance.captures_dir / ".incoming" / "half-written.png").write_bytes(b"\x89PNG")
        (instance.captures_dir / "Notes From The Host.txt").write_text("not a Capture")
        response = api.get("/api/captures")

    assert response.status_code == 200, response.text
    names = [c["name"] for c in response.json()]
    assert names.index(newer) < names.index(older)
    assert not any(name.startswith(".") or "/" in name or name.endswith(".txt") for name in names)
    (instance.captures_dir / "Notes From The Host.txt").unlink()
    (instance.captures_dir / ".incoming" / "half-written.png").unlink()


def test_a_capture_is_served_for_preview_and_downloaded_as_the_original_file(instance, camera):
    image = png(24, 12)
    with instance.client() as api:
        name = snapshot(api, camera, image)
        preview = api.get(f"/api/captures/{name}")
        download = api.get(f"/api/captures/{name}", params={"download": "1"})
        partial = api.get(f"/api/captures/{name}", headers={"Range": "bytes=0-7"})

    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"] == "image/png"
    assert not preview.headers.get("content-disposition", "").startswith("attachment")
    assert preview.content == image

    assert download.status_code == 200, download.text
    assert download.headers["content-disposition"].startswith("attachment")
    assert name in download.headers["content-disposition"]
    assert download.content == image == (instance.captures_dir / name).read_bytes()

    assert partial.status_code == 206  # a browser seeks a Recording's preview by range
    assert partial.content == image[:8]


def test_deleting_a_capture_removes_the_file_from_the_host_folder_and_from_every_list(instance, camera):
    with instance.client() as api:
        name = snapshot(api, camera, png(8, 8))
        keep = snapshot(api, camera, png(9, 9))
        assert (instance.captures_dir / name).exists()

        with instance.events() as events:
            response = api.delete(f"/api/captures/{name}")
            assert response.status_code == 204, response.text
            pushed = events.next("captures", where=lambda data: named(data, name) is None)

        assert not (instance.captures_dir / name).exists()
        assert named(pushed, keep) is not None
        assert named(api.get("/api/captures").json(), name) is None
        assert api.get(f"/api/captures/{name}").status_code == 404
        again = api.delete(f"/api/captures/{name}")
        assert (again.status_code, again.json()["error"]) == (404, "capture_not_found")


@pytest.fixture
def lures(instance, unique_label):
    """Things in and around the captures folder that are not Captures, each
    named so a careless server would serve or delete it."""
    root = instance.captures_dir
    label = unique_label("lure")
    made = {
        "dotfile": root / f".{label}-20260101-000000-8x8.png",
        "staged": root / ".recording" / f"{label}-20260101-000000-8x8.mp4",
        "symlink out": root / f"{label}-out-20260101-000000-8x8.png",
        "relative symlink out": root / f"{label}-rel-20260101-000000-8x8.png",
        "directory": root / f"{label}-dir-20260101-000000-8x8.png",
        "other extension": root / f"{label}-20260101-000000-8x8.json",
    }
    made["dotfile"].write_bytes(b"dot")
    made["staged"].parent.mkdir(exist_ok=True)
    made["staged"].write_bytes(b"staged")
    made["symlink out"].symlink_to("/config/devices.json")  # as the container sees it
    made["relative symlink out"].symlink_to("../config/devices.json")
    made["directory"].mkdir()
    made["other extension"].write_bytes(b"{}")
    yield made
    for path in made.values():
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        else:
            path.unlink(missing_ok=True)


def test_download_and_delete_never_reach_anything_but_a_capture(instance, camera, lures):
    config = (instance.config_dir / "devices.json").read_bytes()
    names = [path.name for path in lures.values()] + [
        "%2E%2E%2Fconfig%2Fdevices.json", "..%2Fconfig%2Fdevices.json", ".recording%2F" + lures["staged"].name,
        "%2E%2E", "devices.json", "session-secret", "UPPER-20260101-000000-8x8.PNG",
    ]
    with instance.client() as api:
        listed = {c["name"] for c in api.get("/api/captures").json()}
        for name in names:
            assert name not in listed
            got = api.get(f"/api/captures/{name}")
            assert got.status_code == 404, (name, got.status_code, got.text[:200])
            gone = api.delete(f"/api/captures/{name}")
            assert gone.status_code in (404, 405), (name, gone.status_code, gone.text[:200])

    for kind, path in lures.items():
        assert path.is_symlink() or path.exists(), kind
    assert (instance.config_dir / "devices.json").read_bytes() == config


@pytest.fixture
def own_device(instance, device_harness, unique_label):
    """A Device this test may pause, on the shared instance under its own label."""
    device = device_harness.add_device(unique_label("held"))
    label = unique_label("held")
    instance.add_device(device, label=label)
    yield device, f"{label}/test-pattern"
    with contextlib.suppress(Exception):
        device.unpause()
    with instance.client() as api:
        api.delete(f"/api/devices/{label}")
    device_harness.remove_device(device)


def recording_state(camera_id, state):
    def matches(cameras):
        found = next((c for c in cameras if c["id"] == camera_id), None)
        return found is not None and (found["recording"] or {}).get("state") == state
    return matches


def test_a_file_of_a_recording_still_running_cannot_be_deleted_until_it_ends(instance, own_device):
    device, camera = own_device
    with instance.events() as events, instance.client() as api:
        started = api.post("/api/recordings", json={"camera": camera})
        assert started.status_code == 201, started.text
        recording = started.json()
        events.next("cameras", where=recording_state(camera, "active"), timeout=20)
        time.sleep(2)
        device.pause()  # the Feed drops: part 1 becomes a Capture, the Recording waits for part 2
        events.next("cameras", where=recording_state(camera, "waiting"), timeout=40)

        [first] = api.get(f"/api/recordings/{recording['id']}").json()["captures"]
        assert named(api.get("/api/captures").json(), first)["in_progress"] is True
        refused = api.delete(f"/api/captures/{first}")
        assert (refused.status_code, refused.json()["error"]) == (409, "capture_recording"), refused.text
        assert (instance.captures_dir / first).exists()

        stopped = api.post(f"/api/recordings/{recording['id']}/stop")
        assert stopped.status_code == 200, stopped.text
        assert named(api.get("/api/captures").json(), first)["in_progress"] is False
        assert api.delete(f"/api/captures/{first}").status_code == 204
    assert not (instance.captures_dir / first).exists()


def test_a_recording_cut_off_by_a_restart_is_listed_as_an_interrupted_recording(
        instances, test_pattern_device, unique_label):
    own = instances.start(unique_label("caps-restart"))
    label = unique_label("cut")
    camera = f"{label}/test-pattern"
    own.add_device(test_pattern_device, label=label)
    with own.events() as events, own.client() as api:
        started = api.post("/api/recordings", json={"camera": camera})
        assert started.status_code == 201, started.text
        events.next("cameras", where=recording_state(camera, "active"), timeout=20)
        time.sleep(3)

    own.restart()

    with own.events() as events, own.client() as api:
        opening = events.next("captures")  # every stream opens with the list
        [entry] = [c for c in opening if c["name"].startswith(f"{label}-")]
        assert (entry["kind"], entry["interrupted"], entry["in_progress"]) == ("recording", True, False)
        assert entry["size"] == (own.captures_dir / entry["name"]).stat().st_size > 0

        served = api.get(f"/api/captures/{entry['name']}")
        assert served.status_code == 200
        assert served.headers["content-type"] == "video/mp4"
        assert served.content == (own.captures_dir / entry["name"]).read_bytes()
