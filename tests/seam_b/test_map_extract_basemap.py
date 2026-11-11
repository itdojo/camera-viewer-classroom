"""Seam B: with a Map Extract installed, Single View's map draws it as the basemap, offline (#61).

The instance has the committed Westminster fixture in its ``maps`` folder (``harness.maps``) and
a Device replaying a fix beside the Thames. The browser can reach that instance and nothing else
(``harness.offline``), so the tiles, style, glyphs and sprites all come from the instance.
"""

import re
import time

import pytest
from playwright.sync_api import expect

from harness.gps import Epoch, track_lines, write_track
from harness.maps import FIXTURE, LATITUDE, LONGITUDE, colour_share, start_with_extracts
from harness.offline import Offline

TRACK = "thames.nmea"
EPOCHS = [Epoch(f"0930{second:02d}.00", LATITUDE, LONGITUDE, 12.0, 0.0, 0.0) for second in range(10)]
WATER = (0x80, 0xDE, 0xEA)  # the Protomaps light flavor's water


@pytest.fixture(scope="module")
def mapped(instances):
    return start_with_extracts(instances, "map-extract-ui", {"westminster.pmtiles": (FIXTURE, time.time())})


@pytest.fixture
def offline_mapped(browser, mapped):
    guard = Offline(browser.new_context(), mapped)
    yield guard
    guard.close()


@pytest.fixture
def thames_device(mapped, device_harness, unique_label, tmp_path):
    tracks = write_track(tmp_path / "tracks", track_lines(EPOCHS), TRACK)
    device = device_harness.add_device(unique_label("thames"), gps=True, gps_tracks=tracks,
                                       settings={"GPS_SOURCE": f"replay:{TRACK}"})
    mapped.add_device(device)
    yield device
    with mapped.client() as api:
        api.delete(f"/api/devices/{device.label}")


def test_the_map_extract_renders_as_the_basemap_with_openstreetmap_attribution(
        offline_mapped, mapped, thames_device):
    page = offline_mapped.new_page()
    page.goto(mapped.base_url)
    page.get_by_role("list", name="Cameras").get_by_role(
        "button", name=re.compile(rf"\b{thames_device.label}\b")).click()
    page.get_by_role("button", name="Management Bar").click()  # it lies over the map's left side
    map_region = page.get_by_role("region", name="Single View").get_by_role("region", name="Map", exact=True)
    expect(map_region.get_by_role("img", name=re.compile(rf"^{thames_device.label}: 51\.50000"))).to_be_visible(
        timeout=15_000)

    attribution = map_region.locator(".maplibregl-ctrl-attrib")
    expect(attribution).to_contain_text("OpenStreetMap")
    expect(attribution).to_contain_text("ODbL")
    expect(attribution).to_be_visible()
    expect(map_region.locator(".map-grid-label")).to_have_count(0)  # the grid is the blank basemap's
    expect(map_region.get_by_role("combobox", name="Basemap")).to_be_visible()  # Streets or Blank (#81)
    body = map_region.locator(".map")
    body.scroll_into_view_if_needed()
    # The Thames is drawn in the extract's water colour once its tiles have loaded.
    deadline = time.monotonic() + 15
    while (share := colour_share(page, body.screenshot(), WATER)) < 0.02 and time.monotonic() < deadline:
        page.wait_for_timeout(500)
    assert share >= 0.02, f"only {share:.1%} of the map is water"
    fetched = page.evaluate("performance.getEntriesByType('resource').map((entry) => new URL(entry.name).pathname)")
    assert "/api/map/extract" in fetched
    assert any(path.endswith(".pbf") for path in fetched)  # the vendored glyphs
    assert offline_mapped.blocked == []
