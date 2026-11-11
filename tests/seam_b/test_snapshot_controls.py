"""Seam B: a Snapshot is the frame on screen at the Feed's full received
resolution, whatever size the video is drawn at, from Single View and from a
Multi View tile."""

from __future__ import annotations

import re
import struct

import pytest
from playwright.sync_api import expect

FEED = (1280, 720)  # the test-pattern Device's resolution

PLAYING = """(video) => video.videoWidth > 0 && video.currentTime > 1"""


def png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR", path
    return struct.unpack(">II", data[16:24])


def snapshots_of(instance, label):
    return [p for p in instance.captures_dir.iterdir() if p.name.startswith(f"{label}-") and p.suffix == ".png"]


@pytest.fixture
def labels(instance, test_pattern_device, unique_label):
    """Add Cameras of their own to the shared instance and remove them afterwards."""
    added = []

    def add(prefix):
        label = unique_label(prefix)
        instance.add_device(test_pattern_device, label=label)
        added.append(label)
        return label

    yield add
    with instance.client() as api:
        for label in added:
            api.delete(f"/api/devices/{label}")


def test_a_snapshot_from_single_view_is_a_png_at_the_feeds_received_resolution(page, instance, labels):
    label = labels("snap-single")
    page.set_viewport_size({"width": 900, "height": 700})  # the video is drawn far smaller than 1280x720
    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(label)).click()
    single = page.get_by_role("region", name="Single View")
    video = single.locator("video")
    video.evaluate(f"v => new Promise(r => {{ const t = setInterval(() => ({PLAYING})(v) && (clearInterval(t), r()), 100) }})")
    drawn = video.evaluate("v => [v.clientWidth, v.clientHeight, v.videoWidth, v.videoHeight]")
    assert drawn[0] < FEED[0] and tuple(drawn[2:]) == FEED

    single.get_by_role("button", name="Snapshot").click()

    expect(single.get_by_role("status")).to_contain_text(re.compile(rf"Saved {label}-test-pattern-.*\.png"),
                                                         timeout=10_000)
    [saved] = snapshots_of(instance, label)
    assert png_size(saved) == FEED
    assert re.fullmatch(rf"{label}-test-pattern-\d{{8}}-\d{{6}}-1280x720\.png", saved.name)
    assert saved.stat().st_size > 20_000, "a frame of the test pattern, not a blank canvas"


def test_a_snapshot_from_a_multi_view_tile_is_a_png_at_the_feeds_received_resolution(page, instance, labels):
    label = labels("snap-tile")
    page.set_viewport_size({"width": 1000, "height": 700})
    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role(
        "checkbox", name=re.compile(rf"\b{label}\b")).check()
    tile = page.get_by_role("region", name="Multi View").get_by_role(
        "article", name=re.compile(rf"\b{label}\b"))
    video = tile.locator("video")
    video.evaluate(f"v => new Promise(r => {{ const t = setInterval(() => ({PLAYING})(v) && (clearInterval(t), r()), 100) }})")
    assert video.evaluate("v => v.clientWidth") < FEED[0]

    tile.get_by_role("button", name="Snapshot").click()

    expect(tile.get_by_role("status")).to_contain_text("Saved", timeout=10_000)
    expect(page.get_by_role("region", name="Multi View")).to_be_visible()  # the click stayed on the tile
    [saved] = snapshots_of(instance, label)
    assert png_size(saved) == FEED
    assert saved.stat().st_size > 20_000, "a frame of the test pattern, not a blank canvas"
