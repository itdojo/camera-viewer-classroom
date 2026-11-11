"""Seam B: the Single View map on the blank basemap, offline (#59).

The Device replays a track heading due north at 20 m/s, one epoch a second, so its marker
moves about 0.00018 degrees of latitude a second. The browser can reach the instance and
nothing else (``harness.offline``), and with no Map Extract and no ``CV_MAP_TILES`` the map
draws on its blank basemap, the only Basemap there is, so there is no Basemap switcher.
"""

import re

import pytest
from playwright.sync_api import expect

from harness.gps import Epoch, track_lines, write_track

TRACK = "north.nmea"
START = 51.50000
STEP = 20 / 111_320  # degrees of latitude in one second at 20 m/s
EPOCHS = [Epoch(f"0930{second:02d}.00", START + second * STEP, -0.12346, 35.0, 20.0, 0.0)
          for second in range(60)]


@pytest.fixture
def gps_device(instance, device_harness, unique_label, tmp_path):
    tracks = write_track(tmp_path / "tracks", track_lines(EPOCHS), TRACK)
    device = device_harness.add_device(unique_label("map"), gps=True, gps_tracks=tracks,
                                       settings={"GPS_SOURCE": f"replay:{TRACK}"})
    instance.add_device(device)
    yield device
    with instance.client() as api:
        api.delete(f"/api/devices/{device.label}")


def single_view(page, instance, label):
    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(rf"\b{label}\b")).click()
    return page.get_by_role("region", name="Single View")


def test_the_map_renders_below_the_feed_with_the_device_marker_scale_bar_grid_and_attribution(
        offline, instance, gps_device):
    page = offline.new_page()
    view = single_view(page, instance, gps_device.label)

    map_region = view.get_by_role("region", name="Map", exact=True)
    expect(map_region).to_be_visible(timeout=15_000)
    marker = map_region.get_by_role("img", name=re.compile(rf"^{gps_device.label}: 51\.50\d+, -0\.12346"))
    expect(marker).to_be_visible(timeout=15_000)
    stage = view.locator(".stage").bounding_box()
    canvas = map_region.locator("canvas")
    expect(canvas).to_be_visible()
    box = canvas.bounding_box()
    assert box["y"] >= stage["y"] + stage["height"]
    assert 300 <= box["height"] <= 340
    expect(map_region.locator(".maplibregl-ctrl-scale")).to_have_text(re.compile(r"^\d+\s(m|km)$"))  # MapLibre puts a no-break space there
    expect(map_region.get_by_text("MapLibre")).to_be_visible()
    # The coordinate grid labels its lines at the map's edges, around where the Device is.
    expect(map_region.get_by_text(re.compile(r"^51\.\d+°N$")).first).to_be_visible()
    expect(map_region.get_by_text(re.compile(r"^0\.\d+°W$")).first).to_be_visible()
    expect(map_region.get_by_role("combobox", name="Basemap")).to_have_count(0)  # Blank is the only one
    assert offline.blocked == []


def open_map(offline, instance, label):
    """Open the Device's Camera in Single View, close the Management Bar (which lies over the
    left of the map), and return (page, map region, its marker)."""
    page = offline.new_page()
    map_region = single_view(page, instance, label).get_by_role("region", name="Map", exact=True)
    page.get_by_role("button", name="Management Bar").click()
    expect(page.get_by_role("complementary", name="Management Bar")).to_be_hidden()
    marker = map_region.get_by_role("img", name=re.compile(rf"^{label}: "))
    expect(marker).to_be_visible(timeout=15_000)
    return page, map_region, marker


def offset_from_centre(map_region, marker):
    """How far, in CSS pixels, the marker sits from the middle of the map (x, y)."""
    area = map_region.locator(".map").bounding_box()
    dot = marker.bounding_box()
    return (dot["x"] + dot["width"] / 2 - (area["x"] + area["width"] / 2),
            dot["y"] + dot["height"] / 2 - (area["y"] + area["height"] / 2))


def centred(offset):
    return abs(offset[0]) < 2 and abs(offset[1]) < 2


def test_the_marker_moves_north_with_the_replay_and_the_map_keeps_it_centred(offline, instance, gps_device):
    page, map_region, marker = open_map(offline, instance, gps_device.label)
    first = marker.get_attribute("aria-label")
    assert first.endswith(", heading 0°")

    expect(marker).not_to_have_attribute("aria-label", first, timeout=5_000)

    latitude = float(re.match(rf"^{gps_device.label}: ([\d.]+),", marker.get_attribute("aria-label"))[1])
    assert START <= latitude <= START + 60 * STEP
    page.wait_for_timeout(300)  # let MapLibre redraw the frame
    assert centred(offset_from_centre(map_region, marker))
    assert offline.blocked == []


def test_dragging_the_map_stops_follow_and_recentre_turns_it_back_on(offline, instance, gps_device):
    page, map_region, marker = open_map(offline, instance, gps_device.label)
    recentre = map_region.get_by_role("button", name="Recentre")
    expect(recentre).to_be_hidden()

    map_region.locator(".map").scroll_into_view_if_needed()  # below the fold under a 16:9 stage
    area = map_region.locator(".map").bounding_box()
    x, y =area["x"] + area["width"] / 2, area["y"] + area["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x - 60, y - 40, steps=8)
    page.mouse.move(x - 120, y - 80, steps=8)
    page.mouse.up()

    expect(recentre).to_be_visible()
    dragged = offset_from_centre(map_region, marker)
    assert dragged[0] < -100 and dragged[1] < -60
    # Fixes keep moving the marker, but the map stays where the viewer left it.
    label = marker.get_attribute("aria-label")
    expect(marker).not_to_have_attribute("aria-label", label, timeout=5_000)
    page.wait_for_timeout(300)
    assert not centred(offset_from_centre(map_region, marker))
    expect(recentre).to_be_visible()

    recentre.click()

    expect(recentre).to_be_hidden()
    page.wait_for_timeout(300)
    assert centred(offset_from_centre(map_region, marker))
    label = marker.get_attribute("aria-label")
    expect(marker).not_to_have_attribute("aria-label", label, timeout=5_000)
    page.wait_for_timeout(300)
    assert centred(offset_from_centre(map_region, marker))  # and it follows again
    assert offline.blocked == []


def test_a_collapsed_map_stays_collapsed_after_a_reload(offline, instance, gps_device):
    page, map_region, marker = open_map(offline, instance, gps_device.label)
    toggle = map_region.get_by_role("button", name="Map", exact=True)
    expect(toggle).to_have_attribute("aria-expanded", "true")

    toggle.click()

    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(map_region.locator("canvas")).to_be_hidden()
    expect(marker).to_be_hidden()

    page.reload()

    expect(map_region).to_be_visible(timeout=15_000)  # Single View and its Camera are view state too
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(map_region.locator("canvas")).to_be_hidden()

    toggle.click()

    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(marker).to_be_visible(timeout=15_000)
    page.wait_for_timeout(300)
    assert centred(offset_from_centre(map_region, marker))
    assert offline.blocked == []


def test_a_device_without_a_position_shows_no_map(offline, instance, test_pattern_device, unique_label):
    label = unique_label("nomap")
    instance.add_device(test_pattern_device, label=label)
    page = offline.new_page()

    view = single_view(page, instance, label)

    expect(view.get_by_role("heading", name=re.compile(label))).to_be_visible()
    page.wait_for_timeout(1_000)  # long enough for a first `positions` event to have drawn a map
    expect(view.get_by_role("region", name="Map", exact=True)).to_be_hidden()
    expect(view.locator("canvas")).to_have_count(0)  # and MapLibre was never started for it
    assert offline.blocked == []
    with instance.client() as api:
        api.delete(f"/api/devices/{label}")
