"""Seam B: 3D buildings on the Map Extract's Streets Basemap (#84, ADR 0015).

The instance has the Westminster Map Extract, whose ``buildings`` layer carries ``height`` (and
``min_height`` on building parts) at both its zooms, 14 and 15, and ``CV_MAP_TILES``, stood in by
the browser with a one-colour PNG as in ``test_basemap_switcher.py``. So it offers Streets,
Streets online and Blank, and only Streets has heights to extrude. The 3D toggle sits beside the
Basemap switcher, shows only on Streets, and is view state, so it survives a reload.

What a viewer sees of 3D buildings is their walls. The flat map has almost no mid-grey (under 1%
of the Westminster map, its labels' halos and edges), while extruded buildings tilted towards the
viewer show shaded grey sides over a fifth of it. So ``walls`` is the share of the map in neutral
mid-greys, and a map is flat again when that share is back down and the map looks as it did
before 3D, compared block by block (``changed``) so that a label placed afresh barely counts while
a tilt, which moves everything, changes most blocks.

In Map View, where follow refits around several markers, the tilt shows in the markers
themselves: they lie on the map, so in 3D each one's square is foreshortened.
"""

import base64
import re
import secrets
import time

import pytest
from playwright.sync_api import expect

from harness.gps import Epoch, track_lines, write_track
from harness.maps import FIXTURE, LATITUDE, LONGITUDE, solid_png, start_with_extracts
from harness.offline import Offline

TILE_ORIGIN = "https://tiles.invalid"
TEMPLATE = f"{TILE_ORIGIN}/{{z}}/{{x}}/{{y}}.png"
MAGENTA = (0xFF, 0x00, 0xFF)  # the stand-in tile
TRACK = "westminster.nmea"
EPOCHS = [Epoch(f"0930{second:02d}.00", LATITUDE, LONGITUDE, 12.0, 0.0, 0.0) for second in range(10)]
WALLS_FLAT = 0.02  # measured 0.5% flat and 22% in 3D on 2026-10-19
CHANGED_FLAT = 0.2  # measured 9% after 3D off, 34% tilted with no buildings, 60% in 3D

# Over a PNG decoded in the page: the share of pixels that are neutral greys from 90 to 190, and,
# given a second PNG of the same size, the share of 16-pixel blocks whose mean colour differs
# between the two by more than 16 levels.
MEASURE = """async ([png, other]) => {
    const pixels = async (data) => {
        const image = new Image();
        image.src = `data:image/png;base64,${data}`;
        await image.decode();
        const canvas = new OffscreenCanvas(image.width, image.height);
        const context = canvas.getContext('2d');
        context.drawImage(image, 0, 0);
        return context.getImageData(0, 0, image.width, image.height);
    };
    const { data, width, height } = await pixels(png);
    let grey = 0;
    for (let i = 0; i < data.length; i += 4) {
        const [r, g, b] = [data[i], data[i + 1], data[i + 2]];
        if (Math.max(r, g, b) - Math.min(r, g, b) <= 10 && r >= 90 && r <= 190) grey += 1;
    }
    if (!other) return { walls: grey / (width * height), changed: null };
    const before = (await pixels(other)).data;
    const SIZE = 16;
    let blocks = 0;
    let changed = 0;
    for (let y = 0; y + SIZE <= height; y += SIZE) {
        for (let x = 0; x + SIZE <= width; x += SIZE) {
            const sums = [0, 0, 0, 0, 0, 0];
            for (let dy = 0; dy < SIZE; dy += 1) {
                for (let dx = 0; dx < SIZE; dx += 1) {
                    const i = ((y + dy) * width + x + dx) * 4;
                    for (let c = 0; c < 3; c += 1) {
                        sums[c] += data[i + c];
                        sums[c + 3] += before[i + c];
                    }
                }
            }
            blocks += 1;
            const differs = [0, 1, 2].some((c) => Math.abs(sums[c] - sums[c + 3]) / (SIZE * SIZE) > 16);
            if (differs) changed += 1;
        }
    }
    return { walls: grey / (width * height), changed: changed / blocks };
}"""


@pytest.fixture(scope="module")
def streets(instances):
    return start_with_extracts(instances, "buildings-3d-ui", {"westminster.pmtiles": (FIXTURE, time.time())},
                               env={"CV_MAP_TILES": TEMPLATE})


@pytest.fixture(scope="module")
def westminster_device(streets, device_harness, tmp_path_factory):
    """One GPS Device for the whole module, since each Device's encode costs a third of a core."""
    tracks = write_track(tmp_path_factory.mktemp("tracks"), track_lines(EPOCHS), TRACK)
    device = device_harness.add_device(f"buildings-{secrets.token_hex(2)}", gps=True, gps_tracks=tracks,
                                       settings={"GPS_SOURCE": f"replay:{TRACK}"})
    streets.add_device(device)
    yield device
    with streets.client() as api:
        api.delete(f"/api/devices/{device.label}")
    device_harness.remove_device(device)


@pytest.fixture
def guard(browser, streets):
    """A browser context that reaches the instance, with the tile server stood in. Returns
    (guard, the tile URLs it answered)."""
    offline = Offline(browser.new_context(), streets)
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
    expect(map_region.get_by_role("img", name=re.compile(rf"^{device.label}: 51\.50000"))).to_be_visible(
        timeout=15_000)
    map_region.locator(".map").scroll_into_view_if_needed()
    return map_region


def toggle(map_region):
    return map_region.get_by_role("button", name="3D buildings")


def switcher(map_region):
    return map_region.get_by_role("combobox", name="Basemap")


def measure(page, png, other=None):
    """``{walls, changed}`` for a screenshot of the map, ``changed`` against ``other`` if given."""
    encode = lambda data: base64.b64encode(data).decode() if data else None  # noqa: E731
    return page.evaluate(MEASURE, [encode(png), encode(other)])


def settled(page, map_region):
    """A screenshot of the map once two taken a second apart agree, so its tiles and labels have
    loaded and any tilt has eased in or out (15 s at most)."""
    body = map_region.locator(".map")
    deadline = time.monotonic() + 15
    shot = body.screenshot()
    while time.monotonic() < deadline:
        page.wait_for_timeout(1_000)
        latest = body.screenshot()
        if measure(page, latest, shot)["changed"] == 0:
            return latest
        shot = latest
    return shot


def test_3d_extrudes_the_extracts_buildings_and_turning_it_off_restores_the_flat_map(guard, streets,
                                                                                     westminster_device):
    offline, tiles = guard
    page = offline.new_page()
    map_region = open_map(page, streets, westminster_device)
    button = toggle(map_region)
    expect(button).to_be_visible()
    expect(button).to_have_attribute("aria-pressed", "false")
    flat = settled(page, map_region)
    assert measure(page, flat)["walls"] < WALLS_FLAT

    button.click()

    expect(button).to_have_attribute("aria-pressed", "true")
    tilted = measure(page, settled(page, map_region), flat)
    assert tilted["walls"] > 5 * WALLS_FLAT, tilted
    assert tilted["changed"] > CHANGED_FLAT, tilted

    button.click()

    expect(button).to_have_attribute("aria-pressed", "false")
    restored = measure(page, settled(page, map_region), flat)
    assert restored["walls"] < WALLS_FLAT, restored
    assert restored["changed"] < CHANGED_FLAT, restored
    assert tiles == []
    assert offline.blocked == []


def test_the_toggle_shows_only_on_streets_and_3d_is_saved_across_basemaps_and_a_reload(guard, streets,
                                                                                       westminster_device):
    offline, _ = guard
    page = offline.new_page()
    map_region = open_map(page, streets, westminster_device)
    toggle(map_region).click()
    expect(toggle(map_region)).to_have_attribute("aria-pressed", "true")
    assert measure(page, settled(page, map_region))["walls"] > 5 * WALLS_FLAT

    switcher(map_region).select_option(label="Streets online")
    expect(toggle(map_region)).to_be_hidden()
    switcher(map_region).select_option(label="Blank")
    expect(toggle(map_region)).to_be_hidden()
    expect(map_region.get_by_text(re.compile(r"^51\.\d+°N$")).first).to_be_visible()  # the grid is Blank's
    assert measure(page, settled(page, map_region))["walls"] < WALLS_FLAT

    switcher(map_region).select_option(label="Streets")
    expect(toggle(map_region)).to_have_attribute("aria-pressed", "true")
    assert measure(page, settled(page, map_region))["walls"] > 5 * WALLS_FLAT

    page.reload()

    expect(map_region.get_by_role("img", name=re.compile(rf"^{westminster_device.label}: 51\.50000"))).to_be_visible(
        timeout=15_000)
    map_region.locator(".map").scroll_into_view_if_needed()
    expect(toggle(map_region)).to_have_attribute("aria-pressed", "true")
    assert measure(page, settled(page, map_region))["walls"] > 5 * WALLS_FLAT
    assert offline.blocked == []


# Fixed Positions for a second Device in Map View: beside the GPS one, then far enough off either
# way to leave the view, so follow refits while the map tilts into 3D or levels out of it.
NEARBY = {"latitude": 51.50100, "longitude": -0.12200}
FAR_EAST = {"latitude": 51.51000, "longitude": -0.10000}
FAR_WEST = {"latitude": 51.49000, "longitude": -0.15000}


@pytest.fixture
def fixed_device(streets, test_pattern_device, unique_label):
    """The label of a Device with no GPS Sidecar on the streets instance, placed beside the GPS one."""
    label = unique_label("buildings-fixed")
    streets.add_device(test_pattern_device, label=label)
    place(streets, label, NEARBY)
    yield label
    with streets.client() as api:
        api.delete(f"/api/devices/{label}")


def place(instance, label, where):
    with instance.client() as api:
        assert api.put(f"/api/devices/{label}/fixed-position", json=where).status_code == 200


def tilted(markers) -> bool | None:
    """True when every marker lies tilted with the map, False when every one faces straight up,
    None in between. A marker turns with the map's pitch, so its 28-pixel square shows only
    cos(50°) of its height in 3D."""
    ratios = [box["height"] / box["width"] for box in (marker.bounding_box() for marker in markers)]
    if all(ratio < 0.8 for ratio in ratios):
        return True
    if all(ratio > 0.95 for ratio in ratios):
        return False
    return None


def all_on_map(region, markers) -> bool:
    area = region.locator(".map").bounding_box()
    for marker in markers:
        box = marker.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        if not (area["x"] <= x <= area["x"] + area["width"] and area["y"] <= y <= area["y"] + area["height"]):
            return False
    return True


def settles(page, region, markers, want_tilted) -> bool:
    """Whether the map comes to rest with every marker in view and tilted as wanted (15 s at
    most), and stays so for two more seconds of fixes."""
    deadline = time.monotonic() + 15
    while not (tilted(markers) is want_tilted and all_on_map(region, markers)):
        if time.monotonic() > deadline:
            return False
        page.wait_for_timeout(250)
    page.wait_for_timeout(2_000)
    return tilted(markers) is want_tilted and all_on_map(region, markers)


def test_in_map_view_3d_tilts_and_levels_out_while_follow_refits(guard, streets, westminster_device, fixed_device):
    offline, _ = guard
    page = offline.new_page()
    page.goto(streets.base_url)
    page.get_by_role("banner").get_by_role("group", name="View").get_by_role("button", name="Map View").click()
    region = page.get_by_role("region", name="Map View")
    markers = [region.get_by_role("button", name=re.compile(rf"^{label}(, \S+)?: "))
               for label in (westminster_device.label, fixed_device)]
    for marker in markers:
        expect(marker).to_be_visible(timeout=20_000)
    page.get_by_role("button", name="Management Bar").click()  # it lies over the map's left side
    assert settles(page, region, markers, want_tilted=False)

    toggle(region).click()
    place(streets, fixed_device, FAR_EAST)  # out of view: follow refits while the tilt eases in

    expect(toggle(region)).to_have_attribute("aria-pressed", "true")
    assert settles(page, region, markers, want_tilted=True), "the map never came to rest tilted around both markers"

    toggle(region).click()
    place(streets, fixed_device, FAR_WEST)  # and again while it eases back to flat

    expect(toggle(region)).to_have_attribute("aria-pressed", "false")
    assert settles(page, region, markers, want_tilted=False), "the map never came to rest flat around both markers"
    assert offline.blocked == []
