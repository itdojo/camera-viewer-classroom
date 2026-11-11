"""Seam B: the Single View header shows the Device's live Position, offline (#55).

The Device replays a one-epoch track, so its Sidecar sends the same fix every second and the
header's text is a known literal. The browser can reach the instance and nothing else
(``harness.offline``).
"""

import re

import pytest
from playwright.sync_api import expect

from harness.gps import Epoch, track_lines, write_track

TRACK = "still.nmea"
# Standing still would mean no heading, so this one moves at 5 m/s (18 km/h) towards 47 degrees.
EPOCH = Epoch("093005.00", 51.50001, -0.12346, 35.4, 5.0, 47.0, satellites=9, hdop=0.8)
STALE_SECONDS = 10  # the shared instance's default


@pytest.fixture
def gps_device(instance, device_harness, unique_label, tmp_path):
    tracks = write_track(tmp_path / "tracks", track_lines([EPOCH]), TRACK)
    device = device_harness.add_device(unique_label("hdr"), gps=True, gps_tracks=tracks,
                                       settings={"GPS_SOURCE": f"replay:{TRACK}"})
    instance.add_device(device)
    yield device
    with instance.client() as api:
        api.delete(f"/api/devices/{device.label}")


def single_view(page, instance, label):
    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(rf"\b{label}\b")).click()
    return page.get_by_role("region", name="Single View").locator("header")


def test_the_header_shows_coordinates_speed_and_heading_with_fix_details_in_a_tooltip(
        offline, instance, gps_device):
    page = offline.new_page()
    header = single_view(page, instance, gps_device.label)

    position = header.get_by_role("group", name="Position")
    expect(position).to_be_visible(timeout=15_000)
    expect(position.get_by_text("51.50001, -0.12346", exact=True)).to_be_visible()
    expect(position).to_contain_text("18.0 km/h")
    expect(position).to_contain_text("47°")
    expect(page.get_by_role("tooltip")).to_be_hidden()

    position.get_by_text("51.50001, -0.12346", exact=True).hover()

    tooltip = page.get_by_role("tooltip")
    expect(tooltip).to_be_visible()
    expect(tooltip).to_have_text("Altitude 35 m · 9 satellites · HDOP 0.8 · GPS time 2026-10-15 09:30:05 UTC")
    assert offline.blocked == []


def test_a_stale_position_greys_out_and_shows_its_age(offline, instance, gps_device):
    page = offline.new_page()
    header = single_view(page, instance, gps_device.label)
    position = header.get_by_role("group", name="Position")
    expect(position).to_be_visible(timeout=15_000)
    expect(position).not_to_have_attribute("data-stale", "")

    # Single View renews Position Demand as it opens, but the first fix may still be the idle
    # rate's, after which a Position goes stale only after 30 + 10 s (#64); on a slow CI runner
    # it was (PR #97). Renew it here too and let a 1 Hz fix arrive, so the 10 s threshold applies.
    with instance.client() as api:
        assert api.put(f"/api/devices/{gps_device.label}/position-demand").status_code == 204
    page.wait_for_timeout(2_000)
    gps_device.stop_gps()

    expect(position).to_have_attribute("data-stale", "", timeout=(STALE_SECONDS + 5) * 1000)
    expect(position.get_by_text("51.50001, -0.12346", exact=True)).to_be_visible()
    expect(position).to_contain_text(re.compile(r"last fix 1\d s ago"))
    expect(position).to_contain_text(re.compile(r"last fix (1[2-9]|2\d) s ago"), timeout=5_000)  # it keeps counting
    assert offline.blocked == []


def test_a_device_without_a_position_shows_none_in_the_header(offline, instance, test_pattern_device, unique_label):
    label = unique_label("nopos")
    instance.add_device(test_pattern_device, label=label)
    page = offline.new_page()

    header = single_view(page, instance, label)

    expect(header.get_by_role("heading", name=re.compile(label))).to_be_visible()
    expect(header.get_by_role("group", name="Position")).to_be_hidden()
    with instance.client() as api:
        api.delete(f"/api/devices/{label}")
