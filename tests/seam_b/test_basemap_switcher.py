"""Seam B: each browser chooses the Basemap its maps draw, from those its instance has (#81).

The instance has the Westminster Map Extract and ``CV_MAP_TILES`` both, so it offers three
Basemaps: Streets from the extract, Streets online and Blank. The tile server is
``tiles.invalid``, stood in by the browser with a one-colour PNG (``Offline.stand_in``), so each
Basemap shows on screen as its own colour: the extract's water, the stand-in's magenta, or neither
with the coordinate grid's labels. Every other origin stays blocked, so ``blocked == []`` still
proves that nothing else left, and an empty list of tile requests proves the extract and Blank
kept every request on the instance. The choice is view state, so it survives a reload; a choice
the instance doesn't offer falls back to the default order of extract, online tiles, blank.
Map View carries the same switcher, and the choice made on either map is drawn by the other.
An instance with a single Basemap shows no switcher (``test_single_view_map.py``,
``test_map_extract_basemap.py``).
"""

import re
import secrets
import time

import pytest
from playwright.sync_api import expect

from harness.gps import Epoch, track_lines, write_track
from harness.maps import FIXTURE, LATITUDE, LONGITUDE, colour_share, solid_png, start_with_extracts
from harness.offline import Offline

TILE_ORIGIN = "https://tiles.invalid"
TEMPLATE = f"{TILE_ORIGIN}/{{z}}/{{x}}/{{y}}.png"
ATTRIBUTION = "Tiles © Example Maps"
MAGENTA = (0xFF, 0x00, 0xFF)  # the stand-in tile, a colour nothing else on the map uses
WATER = (0x80, 0xDE, 0xEA)  # the Map Extract's water (the Protomaps light flavor)
VIEW_STATE = "camera-viewer:view"
TRACK = "thames.nmea"
EPOCHS = [Epoch(f"0930{second:02d}.00", LATITUDE, LONGITUDE, 12.0, 0.0, 0.0) for second in range(10)]


@pytest.fixture(scope="module")
def both(instances):
    return start_with_extracts(instances, "basemap-switcher-ui", {"westminster.pmtiles": (FIXTURE, time.time())},
                               env={"CV_MAP_TILES": TEMPLATE, "CV_MAP_TILES_ATTRIBUTION": ATTRIBUTION})


@pytest.fixture(scope="module")
def thames_device(both, device_harness, tmp_path_factory):
    """One GPS Device for the whole module, since each Device's encode costs a third of a core."""
    tracks = write_track(tmp_path_factory.mktemp("tracks"), track_lines(EPOCHS), TRACK)
    device = device_harness.add_device(f"basemap-{secrets.token_hex(2)}", gps=True, gps_tracks=tracks,
                                       settings={"GPS_SOURCE": f"replay:{TRACK}"})
    both.add_device(device)
    yield device
    with both.client() as api:
        api.delete(f"/api/devices/{device.label}")
    device_harness.remove_device(device)


@pytest.fixture
def guard(browser, both):
    """A browser context that reaches the instance, with the tile server stood in. Returns
    (guard, the tile URLs it answered)."""
    offline = Offline(browser.new_context(), both)
    tiles = offline.stand_in(TILE_ORIGIN, solid_png(MAGENTA), "image/png")
    yield offline, tiles
    offline.close()


def open_map(page, instance, device):
    """Open the Device's Single View map with the Management Bar closed. Returns the map region."""
    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role(
        "button", name=re.compile(rf"\b{device.label}\b")).click()
    page.get_by_role("button", name="Management Bar").click()  # it lies over the map's left side
    map_region = page.get_by_role("region", name="Single View").get_by_role("region", name="Map", exact=True)
    expect(marker_of(map_region, device)).to_be_visible(timeout=15_000)
    return map_region


def marker_of(map_region, device):
    return map_region.get_by_role("img", name=re.compile(rf"^{device.label}: 51\.50000"))


def switcher(map_region):
    return map_region.get_by_role("combobox", name="Basemap")


def shown(select):
    """The name of the Basemap the switcher shows as chosen."""
    return select.evaluate("(select) => select.selectedOptions[0]?.text")


def share_once_drawn(page, map_region, rgb, at_least):
    """The share of the map in ``rgb``, polled until it reaches ``at_least`` or 15 s pass."""
    body = map_region.locator(".map")
    body.scroll_into_view_if_needed()
    deadline = time.monotonic() + 15
    while (share := colour_share(page, body.screenshot(), rgb)) < at_least and time.monotonic() < deadline:
        page.wait_for_timeout(500)
    return share


def offset_from_centre(map_region, marker):
    area = map_region.locator(".map").bounding_box()
    dot = marker.bounding_box()
    return (dot["x"] + dot["width"] / 2 - (area["x"] + area["width"] / 2),
            dot["y"] + dot["height"] / 2 - (area["y"] + area["height"] / 2))


def trail_length(marker):
    return int(marker.get_attribute("data-trail") or 0)


def wait_for_trail(page, marker, more_than, timeout=15):
    deadline = time.monotonic() + timeout
    while (length := trail_length(marker)) <= more_than and time.monotonic() < deadline:
        page.wait_for_timeout(250)
    return length


def test_the_switcher_lists_every_basemap_the_instance_has_and_starts_on_the_extract(guard, both, thames_device):
    offline, tiles = guard
    page = offline.new_page()
    map_region = open_map(page, both, thames_device)
    select = switcher(map_region)

    expect(select).to_be_visible()
    expect(select.get_by_role("option")).to_have_text(["Streets", "Streets online", "Blank"])
    assert shown(select) == "Streets"
    assert share_once_drawn(page, map_region, WATER, 0.02) >= 0.02
    assert tiles == []
    assert offline.blocked == []


def test_switching_changes_only_what_is_under_the_markers_and_the_attribution_follows(guard, both, thames_device):
    offline, tiles = guard
    page = offline.new_page()
    map_region = open_map(page, both, thames_device)
    marker = marker_of(map_region, thames_device)
    attribution = map_region.locator(".maplibregl-ctrl-attrib")
    recentre = map_region.get_by_role("button", name="Recentre")
    scale = map_region.locator(".maplibregl-ctrl-scale")
    expect(attribution).to_contain_text("ODbL")
    trail = wait_for_trail(page, marker, 1)
    assert trail >= 2
    zoom = scale.text_content()

    switcher(map_region).select_option(label="Streets online")

    assert share_once_drawn(page, map_region, MAGENTA, 0.5) >= 0.5
    expect(attribution).to_contain_text(ATTRIBUTION)
    expect(attribution).not_to_contain_text("ODbL")
    expect(attribution).to_contain_text("MapLibre")
    expect(marker).to_be_visible()
    assert wait_for_trail(page, marker, trail) > trail  # the Trail is still drawn, and still grows
    assert scale.text_content() == zoom
    expect(recentre).to_be_hidden()  # still following
    assert all(abs(axis) < 2 for axis in offset_from_centre(map_region, marker))

    # Drag the map away: follow stays off across a switch, and the map stays where it was left.
    map_region.locator(".map").scroll_into_view_if_needed()
    area = map_region.locator(".map").bounding_box()
    x, y = area["x"] + area["width"] / 2, area["y"] + area["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x - 60, y - 40, steps=8)
    page.mouse.move(x - 120, y - 80, steps=8)
    page.mouse.up()
    expect(recentre).to_be_visible()
    page.wait_for_timeout(1_000)  # the drag's inertia eases out
    dragged = offset_from_centre(map_region, marker)
    served = len(tiles)

    switcher(map_region).select_option(label="Blank")

    expect(map_region.get_by_text(re.compile(r"^51\.\d+°N$")).first).to_be_visible()  # the grid is Blank's
    expect(attribution).not_to_contain_text(ATTRIBUTION)
    expect(attribution).not_to_contain_text("ODbL")
    expect(recentre).to_be_visible()
    page.wait_for_timeout(300)
    after = offset_from_centre(map_region, marker)
    assert abs(after[0] - dragged[0]) < 2 and abs(after[1] - dragged[1]) < 2, (dragged, after)

    switcher(map_region).select_option(label="Streets")

    expect(attribution).to_contain_text("ODbL")
    expect(map_region.locator(".map-grid-label")).to_have_count(0)
    expect(marker).to_be_visible()
    assert len(tiles) == served  # Blank and the extract asked the tile server for nothing
    assert offline.blocked == []


def test_the_choice_survives_a_reload(guard, both, thames_device):
    offline, _ = guard
    page = offline.new_page()
    map_region = open_map(page, both, thames_device)
    switcher(map_region).select_option(label="Streets online")
    assert share_once_drawn(page, map_region, MAGENTA, 0.5) >= 0.5

    page.reload()

    expect(marker_of(map_region, thames_device)).to_be_visible(timeout=15_000)
    assert shown(switcher(map_region)) == "Streets online"
    assert share_once_drawn(page, map_region, MAGENTA, 0.5) >= 0.5
    assert "/api/map/extract" not in page.evaluate(
        "performance.getEntriesByType('resource').map((entry) => new URL(entry.name).pathname)")
    assert offline.blocked == []


def test_a_saved_choice_the_instance_does_not_offer_falls_back_to_the_default(guard, both, thames_device):
    offline, tiles = guard
    page = offline.new_page()
    page.goto(both.base_url)
    # A browser that chose Satellite on an instance that has since lost CV_MAP_IMAGERY.
    page.evaluate("""([key, basemap]) => {
        const saved = JSON.parse(localStorage.getItem(key) ?? '{}');
        localStorage.setItem(key, JSON.stringify({ ...saved, basemap }));
    }""", [VIEW_STATE, "imagery"])

    map_region = open_map(page, both, thames_device)

    assert shown(switcher(map_region)) == "Streets"
    assert share_once_drawn(page, map_region, WATER, 0.02) >= 0.02
    assert tiles == []
    assert offline.blocked == []


def map_view_region(page):
    return page.get_by_role("region", name="Map View")


def open_map_view(page, device):
    """Show Map View from the masthead, with the Management Bar closed, once the Device's marker is
    there. Returns the Map View region."""
    page.get_by_role("banner").get_by_role("group", name="View").get_by_role("button", name="Map View").click()
    region = map_view_region(page)
    expect(region.get_by_role("button", name=re.compile(rf"^{device.label}(, \S+)?: 51\.50000"))).to_be_visible(timeout=15_000)
    if page.get_by_role("complementary", name="Management Bar").is_visible():
        page.get_by_role("button", name="Management Bar").click()  # it lies over the map's left side
    return region


def test_map_view_has_the_switcher_and_switching_there_changes_its_basemap(guard, both, thames_device):
    offline, _ = guard
    page = offline.new_page()
    page.goto(both.base_url)
    region = open_map_view(page, thames_device)
    select = switcher(region)
    attribution = region.locator(".maplibregl-ctrl-attrib")
    expect(select.get_by_role("option")).to_have_text(["Streets", "Streets online", "Blank"])
    assert shown(select) == "Streets"
    assert share_once_drawn(page, region, WATER, 0.02) >= 0.02

    select.select_option(label="Streets online")

    assert share_once_drawn(page, region, MAGENTA, 0.5) >= 0.5
    expect(attribution).to_contain_text(ATTRIBUTION)

    select.select_option(label="Blank")

    expect(region.get_by_text(re.compile(r"^51\.\d+°N$")).first).to_be_visible()  # the grid is Blank's
    expect(attribution).not_to_contain_text(ATTRIBUTION)
    expect(attribution).not_to_contain_text("ODbL")
    assert offline.blocked == []


def test_the_basemap_chosen_on_one_map_is_drawn_by_the_other(guard, both, thames_device):
    offline, _ = guard
    page = offline.new_page()
    single_map = open_map(page, both, thames_device)

    switcher(single_map).select_option(label="Streets online")
    assert share_once_drawn(page, single_map, MAGENTA, 0.5) >= 0.5

    region = open_map_view(page, thames_device)  # Single View's map loaded MapLibre, Map View's opens now

    assert shown(switcher(region)) == "Streets online"
    assert share_once_drawn(page, region, MAGENTA, 0.5) >= 0.5

    switcher(region).select_option(label="Blank")
    expect(region.get_by_text(re.compile(r"^51\.\d+°N$")).first).to_be_visible()
    page.get_by_role("button", name="Management Bar").click()  # back to Single View by the Camera list
    page.get_by_role("list", name="Cameras").get_by_role(
        "button", name=re.compile(rf"\b{thames_device.label}\b")).click()
    page.get_by_role("button", name="Management Bar").click()

    expect(single_map.get_by_role("img", name=re.compile(rf"^{thames_device.label}: "))).to_be_visible()
    assert shown(switcher(single_map)) == "Blank"
    expect(single_map.get_by_text(re.compile(r"^51\.\d+°N$")).first).to_be_visible()
    assert offline.blocked == []
