"""Seam B: ``CV_MAP_IMAGERY`` adds a Satellite Basemap to the switcher, never the default (#83).

The instance has ``CV_MAP_TILES`` and ``CV_MAP_IMAGERY`` and no Map Extract, so it offers Streets
online, Satellite and Blank, and a browser that hasn't chosen gets Streets online. Both servers
use names that can never resolve, and ``Offline.stand_in`` answers each in the browser with a PNG
of its own colour, so the Basemap on screen shows as that colour. Every other origin stays
blocked, so ``blocked == []`` still proves that nothing else left. An instance without
``CV_MAP_IMAGERY`` lists no Satellite (``test_basemap_switcher.py``); choosing, the fallback of a
saved Satellite the instance no longer offers, and keeping markers and Trails across a switch are
proved there too, for every Basemap alike.
"""

import re
import secrets
import time

import pytest
from playwright.sync_api import expect

from harness.gps import Epoch, track_lines, write_track
from harness.maps import LATITUDE, LONGITUDE, colour_share, solid_png
from harness.offline import Offline

TILE_ORIGIN = "https://tiles.invalid"
IMAGERY_ORIGIN = "https://imagery.invalid"
# Esri's and The National Map's order, y before x, which the map fills as it is written.
IMAGERY_TEMPLATE = f"{IMAGERY_ORIGIN}/tile/{{z}}/{{y}}/{{x}}"
# Plain text, so the angle brackets show as written rather than being read as markup.
IMAGERY_ATTRIBUTION = "Imagery © <Example Imaging>"
TILES_ATTRIBUTION = "Tiles © Example Maps"
MAGENTA = (0xFF, 0x00, 0xFF)  # the stand-in street tile
GREEN = (0x00, 0xFF, 0x00)  # the stand-in imagery tile; nothing else on the map uses either colour
# The z17 tile holding the Device (51.50000, -0.12346): the map opens at zoom 16, and MapLibre's
# zoom levels are for 512-pixel tiles, so a server of 256-pixel ones is asked for the next zoom up.
DEVICE_TILE = f"{IMAGERY_ORIGIN}/tile/17/43589/65491"
NAIP_LEVELS = 16  # the deepest level The National Map's NAIP cache holds
TRACK = "thames.nmea"
EPOCHS = [Epoch(f"0930{second:02d}.00", LATITUDE, LONGITUDE, 12.0, 0.0, 0.0) for second in range(10)]


@pytest.fixture(scope="module")
def online(instances):
    return instances.start("satellite-ui", env={
        "CV_MAP_TILES": f"{TILE_ORIGIN}/{{z}}/{{x}}/{{y}}.png", "CV_MAP_TILES_ATTRIBUTION": TILES_ATTRIBUTION,
        "CV_MAP_IMAGERY": IMAGERY_TEMPLATE, "CV_MAP_IMAGERY_ATTRIBUTION": IMAGERY_ATTRIBUTION,
    })


@pytest.fixture(scope="module")
def thames_device(online, device_harness, tmp_path_factory):
    tracks = write_track(tmp_path_factory.mktemp("tracks"), track_lines(EPOCHS), TRACK)
    device = device_harness.add_device(f"satellite-{secrets.token_hex(2)}", gps=True, gps_tracks=tracks,
                                       settings={"GPS_SOURCE": f"replay:{TRACK}"})
    online.add_device(device)
    yield device
    with online.client() as api:
        api.delete(f"/api/devices/{device.label}")
    device_harness.remove_device(device)


@pytest.fixture
def guard(browser, online):
    """A browser context that reaches the instance, with both servers stood in. Returns
    (guard, the street tile URLs it answered, the imagery URLs it answered)."""
    offline = Offline(browser.new_context(), online)
    tiles = offline.stand_in(TILE_ORIGIN, solid_png(MAGENTA), "image/png")
    imagery = offline.stand_in(IMAGERY_ORIGIN, solid_png(GREEN), "image/png")
    yield offline, tiles, imagery
    offline.close()


def open_map(page, instance, device):
    """Open the Device's Single View map with the Management Bar closed. Returns the map region."""
    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role(
        "button", name=re.compile(rf"\b{device.label}\b")).click()
    page.get_by_role("button", name="Management Bar").click()  # it lies over the map's left side
    map_region = page.get_by_role("region", name="Single View").get_by_role("region", name="Map", exact=True)
    expect(map_region.get_by_role("img", name=re.compile(rf"^{device.label}: 51\.50000"))).to_be_visible(
        timeout=15_000)
    return map_region


def switcher(map_region):
    return map_region.get_by_role("combobox", name="Basemap")


def share_once_drawn(page, map_region, rgb, at_least):
    """The share of the map in ``rgb``, polled until it reaches ``at_least`` or 15 s pass."""
    body = map_region.locator(".map")
    body.scroll_into_view_if_needed()
    deadline = time.monotonic() + 15
    while (share := colour_share(page, body.screenshot(), rgb)) < at_least and time.monotonic() < deadline:
        page.wait_for_timeout(500)
    return share


def test_satellite_is_offered_but_never_the_default(guard, online, thames_device):
    offline, tiles, imagery = guard
    page = offline.new_page()
    map_region = open_map(page, online, thames_device)
    select = switcher(map_region)

    expect(select.get_by_role("option")).to_have_text(["Streets online", "Satellite", "Blank"])
    assert select.evaluate("(select) => select.selectedOptions[0]?.text") == "Streets online"
    assert share_once_drawn(page, map_region, MAGENTA, 0.5) >= 0.5
    assert tiles
    assert imagery == []
    assert offline.blocked == []


def test_choosing_satellite_draws_the_imagery_with_its_attribution_and_survives_a_reload(guard, online, thames_device):
    offline, tiles, imagery = guard
    page = offline.new_page()
    map_region = open_map(page, online, thames_device)
    attribution = map_region.locator(".maplibregl-ctrl-attrib")

    switcher(map_region).select_option(label="Satellite")

    share = share_once_drawn(page, map_region, GREEN, 0.5)
    assert share >= 0.5, f"only {share:.1%} of the map is the stand-in imagery"
    assert DEVICE_TILE in imagery
    assert all(re.fullmatch(rf"{re.escape(IMAGERY_ORIGIN)}/tile/\d+/\d+/\d+", url) for url in imagery), imagery
    expect(attribution).to_be_visible()
    expect(attribution).to_contain_text(IMAGERY_ATTRIBUTION)
    expect(attribution).not_to_contain_text(TILES_ATTRIBUTION)
    expect(attribution).to_contain_text("MapLibre")
    expect(map_region.locator(".map-grid-label")).to_have_count(0)  # the grid is the blank basemap's

    page.reload()
    served = len(tiles)

    expect(map_region.get_by_role("img", name=re.compile(rf"^{thames_device.label}: 51\.50000"))).to_be_visible(
        timeout=15_000)
    assert switcher(map_region).evaluate("(select) => select.selectedOptions[0]?.text") == "Satellite"
    assert share_once_drawn(page, map_region, GREEN, 0.5) >= 0.5
    expect(attribution).to_contain_text(IMAGERY_ATTRIBUTION)
    assert len(tiles) == served  # the street tiles weren't asked for after the reload
    assert offline.blocked == []


def imagery_level(url):
    return int(re.search(r"/tile/(\d+)/", url).group(1))


def test_past_the_servers_deepest_level_the_map_enlarges_the_deepest_imagery_it_has(browser, online, thames_device):
    # NAIP from The National Map stops at level 16 and answers 404 below it. The map asks for
    # deeper levels anyway, since other imagery servers go to 19.
    offline = Offline(browser.new_context(), online)
    offline.stand_in(TILE_ORIGIN, solid_png(MAGENTA), "image/png")
    imagery = offline.stand_in(IMAGERY_ORIGIN, solid_png(GREEN), "image/png",
                               missing=lambda url: imagery_level(url) > NAIP_LEVELS)
    try:
        page = offline.new_page()
        map_region = open_map(page, online, thames_device)

        switcher(map_region).select_option(label="Satellite")

        # The map opens at zoom 16, which asks for level 17: 404, so it draws level 16 enlarged.
        share = share_once_drawn(page, map_region, GREEN, 0.5)
        assert share >= 0.5, f"only {share:.1%} of the map is imagery"
        assert DEVICE_TILE in imagery
        assert any(imagery_level(url) == NAIP_LEVELS for url in imagery), imagery

        # Zoom in until the map asks for level 19, from zoom 18 on. Follow centres the marker on
        # each fix with a jump, which cuts a zoom's ease short, so it can take more than 3 clicks.
        zoom_in = map_region.get_by_role("button", name="Zoom in")
        deadline = time.monotonic() + 20
        while not any(imagery_level(url) == 19 for url in imagery):
            assert time.monotonic() < deadline, sorted({imagery_level(url) for url in imagery})
            if zoom_in.is_enabled():  # MapLibre disables it at the closest zoom
                zoom_in.click()
            page.wait_for_timeout(750)
        page.wait_for_timeout(1_000)  # every level-19 request has had its 404
        share = share_once_drawn(page, map_region, GREEN, 0.5)
        assert share >= 0.5, f"only {share:.1%} of the map is imagery at zoom 19"
        assert offline.blocked == []
    finally:
        offline.close()
