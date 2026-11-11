"""Seam B: each Multi View tile shows its Camera's Position and how far to trust it, offline (#56).

The fix-status indicator reads live, stale with its age, or none (#58 adds fixed). The Device
replays a one-epoch track, so its Sidecar sends the same fix every second and the coordinates are
a known literal. The browser can reach the instance and nothing else (``harness.offline``).
"""

import re

import pytest
from playwright.sync_api import expect

from harness.gps import Epoch, track_lines, write_track

TRACK = "still.nmea"
EPOCH = Epoch("093005.00", 51.50001, -0.12346, 35.4, 5.0, 47.0)
COORDINATES = "51.50001, -0.12346"
STALE_SECONDS = 10  # the shared instance's default


@pytest.fixture
def gps_device(instance, device_harness, unique_label, tmp_path):
    tracks = write_track(tmp_path / "tracks", track_lines([EPOCH]), TRACK)
    device = device_harness.add_device(unique_label("tile"), gps=True, gps_tracks=tracks,
                                       settings={"GPS_SOURCE": f"replay:{TRACK}"})
    instance.add_device(device)
    yield device
    with instance.client() as api:
        api.delete(f"/api/devices/{device.label}")


def tile_on_screen(page, instance, label):
    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role("checkbox", name=re.compile(rf"\b{label}\b")).check()
    tile = page.get_by_role("region", name="Multi View").get_by_role("article", name=re.compile(rf"\b{label}\b"))
    expect(tile).to_be_visible()
    return tile


def test_a_tile_shows_a_live_fix_then_a_stale_one_with_its_age(offline, instance, gps_device):
    page = offline.new_page()
    tile = tile_on_screen(page, instance, gps_device.label)

    position = tile.get_by_role("group", name="Position")
    expect(position.get_by_text(COORDINATES, exact=True)).to_be_visible(timeout=15_000)
    fix = position.locator("[data-fix]")  # no live role: a ticking age would be announced every second
    expect(fix).to_have_attribute("data-fix", "live")
    expect(fix).to_have_text("GPS")
    expect(fix).to_have_attribute("title", "Live GPS fix")
    expect(position).not_to_have_attribute("data-stale", "")
    entry = page.get_by_role("list", name="Devices").get_by_role("listitem").filter(
        has=page.get_by_text(gps_device.label, exact=True))
    summary = entry.locator("summary")
    expect(summary).to_have_text(f"PositionGPS{COORDINATES}")  # the Devices Panel agrees
    # A fresh 3D fix is the Position, so the Fixed Position form stays shut.
    expect(summary).to_have_attribute("aria-disabled", "true")
    summary.click()
    expect(entry.get_by_role("form", name=f"Fixed Position of {gps_device.label}")).to_be_hidden()

    # Multi View alone leaves the Device at its idle rate, where a Position goes stale only after
    # 30 + 10 s (#64). Stand in for a viewer with it in Single View elsewhere: renew its Position
    # Demand, let a 1 Hz fix arrive, and the 10 s threshold applies, well within the 25 s linger.
    with instance.client() as api:
        assert api.put(f"/api/devices/{gps_device.label}/position-demand").status_code == 204
    page.wait_for_timeout(2_000)
    gps_device.stop_gps()

    expect(fix).to_have_attribute("data-fix", "stale", timeout=(STALE_SECONDS + 5) * 1000)
    expect(position).to_have_attribute("data-stale", "")
    expect(position.get_by_text(COORDINATES, exact=True)).to_be_visible()  # the last fix stays on screen
    expect(fix).to_have_text(re.compile(r"last fix 1\d s ago"))
    expect(fix).to_have_attribute("title", re.compile(r"Last GPS fix 1\d s ago"))
    expect(fix).to_have_text(re.compile(r"last fix (1[2-9]|2\d) s ago"), timeout=5_000)  # it keeps counting
    expect(summary).not_to_have_attribute("aria-disabled", "true")  # a stale fix opens the form again
    summary.click()
    expect(entry.get_by_role("form", name=f"Fixed Position of {gps_device.label}")).to_be_visible()
    assert offline.blocked == []


def test_a_tile_of_a_device_without_a_position_shows_none(offline, instance, test_pattern_device, unique_label):
    label = unique_label("nopos")
    instance.add_device(test_pattern_device, label=label)
    page = offline.new_page()

    tile = tile_on_screen(page, instance, label)

    position = tile.get_by_role("group", name="Position")
    fix = position.locator("[data-fix]")
    expect(fix).to_have_attribute("data-fix", "none")
    expect(fix).to_have_text("No Position")
    expect(position).to_have_text("No Position")  # no coordinates beside it
    assert offline.blocked == []
    with instance.client() as api:
        api.delete(f"/api/devices/{label}")
