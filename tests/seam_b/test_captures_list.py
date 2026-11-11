"""Seam B: the Captures list — every Recording and Snapshot, newest first,
arriving in every browser without a reload, with a preview, a download of
the original file, and a delete that asks first. Interrupted Recordings are
flagged."""

from __future__ import annotations

import re
import struct
import time
import zlib

import pytest
from playwright.sync_api import expect


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(width: int, height: int) -> bytes:
    rows = b"".join(b"\x00" + bytes((0, 168, 132)) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


@pytest.fixture
def camera(instance, test_pattern_device, unique_label):
    label = unique_label("caplist")
    instance.add_device(test_pattern_device, label=label)
    yield f"{label}/test-pattern"
    with instance.client() as api:
        api.delete(f"/api/devices/{label}")


def snapshot_from_another_laptop(instance, camera_id, image):
    with instance.client() as api:  # its own session: not the browser under test
        response = api.post("/api/snapshots", params={"camera": camera_id}, content=image,
                            headers={"Content-Type": "image/png"})
    assert response.status_code == 201, response.text
    return response.json()["capture"]


def capture_row(page, name):
    return page.get_by_role("list", name="Captures").get_by_role("listitem").filter(has_text=name)


def test_a_snapshot_taken_elsewhere_appears_without_reloading_and_downloads_as_the_original(
        page, instance, camera):
    page.goto(instance.base_url)
    expect(page.get_by_role("list", name="Captures")).to_be_attached()

    image = png(40, 30)
    name = snapshot_from_another_laptop(instance, camera, image)

    row = capture_row(page, name)
    expect(row).to_be_visible(timeout=10_000)
    expect(row).to_contain_text("Snapshot")
    expect(row).to_contain_text(f"{len(image)} B")

    row.get_by_role("button", name="Preview").click()
    preview = page.get_by_role("dialog", name=re.compile(re.escape(name)))
    expect(preview).to_be_visible()
    shown = preview.get_by_role("img")
    expect(shown).to_be_visible()
    shown.evaluate("img => img.decode()")
    assert shown.evaluate("img => [img.naturalWidth, img.naturalHeight]") == [40, 30]
    preview.get_by_role("button", name="Close").click()
    expect(preview).to_be_hidden()

    with page.expect_download() as downloading:
        row.get_by_role("link", name="Download").click()
    download = downloading.value
    assert download.suggested_filename == name
    with open(download.path(), "rb") as saved:
        assert saved.read() == image


def test_the_newest_capture_is_listed_first(page, instance, camera):
    page.goto(instance.base_url)
    older = snapshot_from_another_laptop(instance, camera, png(8, 8))
    time.sleep(0.05)
    newer = snapshot_from_another_laptop(instance, camera, png(9, 9))
    items = page.get_by_role("list", name="Captures").get_by_role("listitem")
    expect(items.filter(has_text=newer)).to_be_visible(timeout=10_000)
    texts = items.all_text_contents()
    order = [next(i for i, text in enumerate(texts) if name in text) for name in (newer, older)]
    assert order[0] < order[1], texts


def test_delete_asks_for_confirmation_then_removes_the_file_from_the_host_folder(page, instance, camera):
    page.goto(instance.base_url)
    name = snapshot_from_another_laptop(instance, camera, png(12, 12))
    row = capture_row(page, name)
    expect(row).to_be_visible(timeout=10_000)

    row.get_by_role("button", name="Delete").click()
    asking = page.get_by_role("alertdialog", name=re.compile(re.escape(name)))
    expect(asking).to_be_visible()
    asking.get_by_role("button", name="Cancel").click()
    expect(asking).to_be_hidden()
    expect(row).to_be_visible()
    assert (instance.captures_dir / name).exists(), "Cancel must leave the file alone"

    row.get_by_role("button", name="Delete").click()
    expect(asking).to_be_visible()
    asking.get_by_role("button", name="Delete").click()

    expect(asking).to_be_hidden()
    expect(row).to_have_count(0, timeout=10_000)
    assert not (instance.captures_dir / name).exists()


def test_an_interrupted_recording_is_flagged_and_its_preview_plays(
        page, instances, test_pattern_device, unique_label):
    own = instances.start(unique_label("caplist-restart"))
    label = unique_label("cutoff")
    camera = f"{label}/test-pattern"
    own.add_device(test_pattern_device, label=label)
    with own.events() as events, own.client() as api:
        started = api.post("/api/recordings", json={"camera": camera})
        assert started.status_code == 201, started.text
        events.next("cameras", timeout=20, where=lambda cameras: any(
            c["id"] == camera and (c["recording"] or {}).get("state") == "active" for c in cameras))
        time.sleep(4)
    own.restart()

    page.goto(own.base_url)
    row = page.get_by_role("list", name="Captures").get_by_role("listitem").filter(has_text=f"{label}-")
    expect(row).to_be_visible(timeout=10_000)
    expect(row).to_contain_text("Recording")
    expect(row.get_by_text("Interrupted", exact=True)).to_be_visible()

    row.get_by_role("button", name="Preview").click()
    video = page.get_by_role("dialog").locator("video")
    expect(video).to_be_visible()
    video.evaluate("v => new Promise((resolve, reject) => {"
                   " const t = setInterval(() => v.currentTime > 1 && (clearInterval(t), resolve()), 100);"
                   " setTimeout(() => reject(new Error('the Recording did not play: ' + v.error?.message)), 15000); })")
    assert video.evaluate("v => [v.videoWidth, v.videoHeight]") == [1280, 720]
