"""Seam B: every Device's Trail behind its marker in Map View, offline on the blank basemap (#79).

Map View keeps Trails exactly as Single View's map does (#60, trails.js): fetched per Device when
Map View opens and extended from ``positions``. The line is on the canvas, so each marker element
carries ``data-trail``, how many fixes its Trail line holds (0 until it has two). The Devices
replay tracks one epoch a second, and the tests renew their Position Demand over the API, standing
in for a viewer elsewhere, so each Trail grows by about one fix a second.
"""

from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import Page, expect

from harness.gps import Epoch, track_lines, write_track
from harness.maps import colour_share

STEP = 20 / 111_320  # degrees of latitude in one second at 20 m/s
NORTH = [Epoch(f"1100{second:02d}.00", 51.50000 + second * STEP, -0.12346, 35.0, 20.0, 0.0)
         for second in range(60)]
SOUTH = [Epoch(f"1100{second:02d}.00", 51.51000 - second * STEP, -0.10000, 20.0, 20.0, 180.0)
         for second in range(60)]
FIXED = {"latitude": 51.60000, "longitude": -0.30000}
HISTORY = 12  # fixes the instance holds for each Device before the page opens
# A Trail on the blank basemap in the light theme: --jade (#3CB858) at 0.7 opacity over
# --surface-card (#FFFFFF).
TRAIL_RGB = (118, 205, 138)


@pytest.fixture
def moving_device(instance, device_harness, unique_label, tmp_path):
    """A factory for GPS Devices replaying a track, removed (Device Kit stack too) at teardown."""
    devices = []

    def add(name, epochs):
        tracks = write_track(tmp_path / f"tracks-{name}", track_lines(epochs), f"{name}.nmea")
        device = device_harness.add_device(unique_label(f"trail-{name}"), gps=True, gps_tracks=tracks,
                                           settings={"GPS_SOURCE": f"replay:{name}.nmea"})
        instance.add_device(device)
        devices.append(device)
        return device

    yield add
    with instance.client() as api:
        for device in devices:
            api.delete(f"/api/devices/{device.label}")
    for device in devices:
        device_harness.remove_device(device)  # each encodes 720p in software


def renew(instance, *labels):
    with instance.client() as api:
        for label in labels:
            assert api.put(f"/api/devices/{label}/position-demand").status_code == 204


def wait_for_history(instance, labels, count=HISTORY, timeout=40):
    # An unwatched Device streams a fix every 30 s, and Map View's own renewal is #78's, so
    # renew here: HISTORY fixes then take HISTORY seconds, within the linger.
    renew(instance, *labels)
    deadline = time.monotonic() + timeout
    with instance.client() as api:
        for label in labels:
            while len(api.get(f"/api/devices/{label}/trail").json()["fixes"]) < count:
                assert time.monotonic() < deadline
                time.sleep(0.5)


def marker(page: Page, label: str):
    return page.get_by_role("region", name="Map View").get_by_role("button", name=re.compile(rf"^{label}(, \S+)?: "))


def open_map_view(page: Page, instance, labels) -> None:
    page.goto(instance.base_url)
    page.get_by_role("banner").get_by_role("group", name="View").get_by_role("button", name="Map View").click()
    for label in labels:
        expect(marker(page, label)).to_be_visible(timeout=20_000)


def trail_length(page: Page, label: str) -> int:
    return int(marker(page, label).get_attribute("data-trail") or 0)


def wait_for_trail(page: Page, label: str, count: int, timeout: float) -> int:
    """Wait until the marker's Trail line holds ``count`` fixes or more; return how many."""
    deadline = time.monotonic() + timeout
    while (length := trail_length(page, label)) < count:
        assert time.monotonic() < deadline, f"{label}'s Trail holds {length} fixes, not {count}"
        page.wait_for_timeout(100)
    return length


def test_every_moving_device_draws_its_trail_from_history_and_extends_it_live(offline, instance, moving_device):
    labels = [moving_device("north", NORTH).label, moving_device("south", SOUTH).label]
    wait_for_history(instance, labels)
    page = offline.new_page()

    opened = time.monotonic()
    open_map_view(page, instance, labels)

    # Live fixes alone would take HISTORY seconds to make Trails this long.
    for label in labels:
        wait_for_trail(page, label, HISTORY, timeout=HISTORY - (time.monotonic() - opened) - 1)
    grown = {label: trail_length(page, label) for label in labels}
    renew(instance, *labels)
    for label in labels:
        wait_for_trail(page, label, grown[label] + 2, timeout=10)  # and live fixes extend each one
    assert offline.blocked == []


def test_a_fixed_position_never_draws_a_trail(offline, instance, moving_device):
    device = moving_device("north", NORTH)
    wait_for_history(instance, [device.label], count=3)
    page = offline.new_page()
    open_map_view(page, instance, [device.label])
    wait_for_trail(page, device.label, 3, timeout=10)

    # Once its last fix is stale the Device shows its Fixed Position, while the instance still
    # holds a Trail of fixes from elsewhere.
    with instance.client() as api:
        assert api.put(f"/api/devices/{device.label}/fixed-position", json=FIXED).status_code == 200
    renew(instance, device.label)  # stale after POSITION_STALE_SECONDS, not the idle rate's 40 s
    device.stop_gps()

    expect(marker(page, device.label)).to_have_accessible_name(re.compile(r": 51\.60000, -0\.30000\b"), timeout=20_000)
    expect(marker(page, device.label)).to_have_attribute("data-trail", "0")
    page.wait_for_timeout(1_000)
    expect(marker(page, device.label)).to_have_attribute("data-trail", "0")
    with instance.client() as api:
        assert len(api.get(f"/api/devices/{device.label}/trail").json()["fixes"]) >= 3
    assert offline.blocked == []


def test_a_removed_device_takes_its_marker_and_trail_off_map_view(offline, instance, moving_device):
    gone, kept = moving_device("north", NORTH).label, moving_device("south", SOUTH).label
    wait_for_history(instance, [gone, kept], count=6)
    page = offline.new_page()
    open_map_view(page, instance, [gone, kept])
    page.get_by_role("button", name="Management Bar").click()  # it lies over the map's left side
    for label in (gone, kept):
        wait_for_trail(page, label, 6, timeout=10)
    page.wait_for_timeout(300)

    # The north-bound Device's Trail runs south of its marker, down the screen.
    box = marker(page, gone).bounding_box()
    behind = {"x": box["x"] + box["width"] / 2 - 15, "y": box["y"] + box["height"], "width": 30, "height": 100}
    drawn = colour_share(page, page.screenshot(clip=behind), TRAIL_RGB)
    assert drawn > 0.02, f"only {drawn:.1%} of the area behind the marker is Trail"

    with instance.client() as api:
        api.delete(f"/api/devices/{gone}").raise_for_status()

    expect(marker(page, gone)).to_have_count(0, timeout=10_000)
    page.wait_for_timeout(500)
    left = colour_share(page, page.screenshot(clip=behind), TRAIL_RGB)
    assert left < 0.002, f"{left:.1%} of the area behind the removed marker is still Trail"
    expect(marker(page, kept)).to_be_visible()
    assert trail_length(page, kept) >= 6
    assert offline.blocked == []
