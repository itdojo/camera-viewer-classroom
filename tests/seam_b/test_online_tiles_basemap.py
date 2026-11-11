"""Seam B: ``CV_MAP_TILES`` gives Single View's map an online basemap when there is no Map Extract,
and a browser that hasn't chosen a Basemap still gets the Map Extract first when there is one
(#62, #81; choosing is ``test_basemap_switcher.py``).

The tile server is ``tiles.invalid``, a name that can never resolve, and the browser never asks
it anything: ``Offline.stand_in`` answers its requests in the browser with a one-colour PNG, so
the online basemap shows on screen as that colour. Every other origin but the instance stays
blocked, so ``blocked == []`` still proves nothing else left. With neither an extract nor
``CV_MAP_TILES`` the map is blank, which ``test_single_view_map.py`` covers on the shared instance.
"""

import re
import time

import pytest
from playwright.sync_api import expect

from harness.gps import Epoch, track_lines, write_track
from harness.maps import FIXTURE, LATITUDE, LONGITUDE, colour_share, solid_png, start_with_extracts
from harness.offline import Offline

TILE_ORIGIN = "https://tiles.invalid"
TEMPLATE = f"{TILE_ORIGIN}/{{z}}/{{x}}/{{y}}.png"
# Plain text, so the angle brackets show as written rather than being read as markup.
ATTRIBUTION = "Tiles © <Example Maps>"
MAGENTA = (0xFF, 0x00, 0xFF)  # the stand-in tile, a colour nothing else on the map uses
WATER = (0x80, 0xDE, 0xEA)  # the Map Extract's water (the Protomaps light flavor)
# The z17 tile holding the Device (51.50000, -0.12346), by the OpenStreetMap wiki's slippy-map
# formula. The map opens at zoom 16 centred on it, and MapLibre's zoom levels are for 512-pixel
# tiles, so a server of 256-pixel ones is asked for the next zoom up.
DEVICE_TILE = f"{TILE_ORIGIN}/17/65491/43589.png"
TRACK = "thames.nmea"
EPOCHS = [Epoch(f"0930{second:02d}.00", LATITUDE, LONGITUDE, 12.0, 0.0, 0.0) for second in range(10)]


@pytest.fixture(scope="module")
def tiled(instances):
    return instances.start("online-tiles-ui", env={"CV_MAP_TILES": TEMPLATE, "CV_MAP_TILES_ATTRIBUTION": ATTRIBUTION})


@pytest.fixture(scope="module")
def tiled_with_extract(instances):
    return start_with_extracts(instances, "online-tiles-and-extract-ui",
                               {"westminster.pmtiles": (FIXTURE, time.time())}, env={"CV_MAP_TILES": TEMPLATE})


def device_on(instance, device_harness, label, tracks_dir):
    tracks = write_track(tracks_dir, track_lines(EPOCHS), TRACK)
    device = device_harness.add_device(label, gps=True, gps_tracks=tracks,
                                       settings={"GPS_SOURCE": f"replay:{TRACK}"})
    instance.add_device(device)
    yield device
    with instance.client() as api:
        api.delete(f"/api/devices/{device.label}")


@pytest.fixture
def tiled_device(tiled, device_harness, unique_label, tmp_path):
    yield from device_on(tiled, device_harness, unique_label("tiles"), tmp_path / "tracks")


@pytest.fixture
def extract_device(tiled_with_extract, device_harness, unique_label, tmp_path):
    yield from device_on(tiled_with_extract, device_harness, unique_label("tiles"), tmp_path / "tracks")


@pytest.fixture
def guards(browser):
    """Make offline contexts for an instance, each with the tile server stood in; closed after."""
    made = []

    def offline_for(instance):
        guard = Offline(browser.new_context(), instance)
        made.append(guard)
        return guard, guard.stand_in(TILE_ORIGIN, solid_png(MAGENTA), "image/png")

    yield offline_for
    for guard in made:
        guard.close()


def open_map(guard, instance, device):
    """Open the Device's Single View map, with the Management Bar closed. Returns (page, map)."""
    page = guard.new_page()
    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role(
        "button", name=re.compile(rf"\b{device.label}\b")).click()
    page.get_by_role("button", name="Management Bar").click()  # it lies over the map's left side
    map_region = page.get_by_role("region", name="Single View").get_by_role("region", name="Map", exact=True)
    expect(map_region.get_by_role("img", name=re.compile(rf"^{device.label}: 51\.50000"))).to_be_visible(
        timeout=15_000)
    return page, map_region


def share_once_drawn(page, map_region, rgb, at_least):
    """The share of the map in ``rgb``, polled until it reaches ``at_least`` or 15 s pass."""
    body = map_region.locator(".map")
    body.scroll_into_view_if_needed()
    deadline = time.monotonic() + 15
    while (share := colour_share(page, body.screenshot(), rgb)) < at_least and time.monotonic() < deadline:
        page.wait_for_timeout(500)
    return share


def fetched_paths(page):
    return page.evaluate("performance.getEntriesByType('resource').map((entry) => new URL(entry.name).pathname)")


def test_without_an_extract_the_map_draws_the_online_tiles_with_their_attribution(guards, tiled, tiled_device):
    guard, tiles = guards(tiled)
    page, map_region = open_map(guard, tiled, tiled_device)

    share = share_once_drawn(page, map_region, MAGENTA, 0.5)

    assert share >= 0.5, f"only {share:.1%} of the map is the stand-in tile"
    assert DEVICE_TILE in tiles
    assert all(re.fullmatch(rf"{re.escape(TILE_ORIGIN)}/\d+/\d+/\d+\.png", url) for url in tiles), tiles
    attribution = map_region.locator(".maplibregl-ctrl-attrib")
    expect(attribution).to_be_visible()
    expect(attribution).to_contain_text(ATTRIBUTION)
    expect(attribution).to_contain_text("MapLibre")
    expect(map_region.locator(".map-grid-label")).to_have_count(0)  # the grid is the blank basemap's
    assert "/api/map/extract" not in fetched_paths(page)
    assert guard.blocked == []


def test_with_no_saved_choice_a_map_extract_comes_before_the_online_tiles(guards, tiled_with_extract, extract_device):
    guard, tiles = guards(tiled_with_extract)
    page, map_region = open_map(guard, tiled_with_extract, extract_device)

    share = share_once_drawn(page, map_region, WATER, 0.02)

    assert share >= 0.02, f"only {share:.1%} of the map is the extract's water"
    assert "/api/map/extract" in fetched_paths(page)
    expect(map_region.locator(".maplibregl-ctrl-attrib")).to_contain_text("ODbL")
    assert tiles == []
    assert guard.blocked == []
