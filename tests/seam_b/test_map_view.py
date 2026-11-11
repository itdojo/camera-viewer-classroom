"""Seam B: Map View (#77), the third View, offline on the blank basemap.

Two GPS Devices replay their tracks for the whole module: one heads due north from Westminster
at 20 m/s, the other stands still about 1.6 km east of it. A third, joining Device is the shared
test-pattern Device under a new label with a Fixed Position, which a test can move at will, so
"refits as a Device joins or moves" needs no wait for a fix. Map View markers are buttons named
as the Single View marker is, with the Device's address after the label (#96), ("<label>,
<address>: <lat>, <lon>, heading <n>°"), since choosing one opens the Map Preview
(`test_map_preview.py`), with their worst Feed Status after it (#80, `test_map_marker_states.py`).
"""

from __future__ import annotations

import re
import secrets
import time

import pytest
from playwright.sync_api import Page, expect

from harness.gps import Epoch, track_lines, write_track
from harness.map_view import close_bar, map_view, marker, open_map_view, place

NORTH = [Epoch(f"0930{second:02d}.00", 51.50000 + second * 20 / 111_320, -0.12346, 35.0, 20.0, 0.0)
         for second in range(60)]
STILL = [Epoch(f"0930{second:02d}.00", 51.50500, -0.10000, 20.0, 0.0, 90.0) for second in range(60)]
JOINED = {"latitude": 51.52000, "longitude": -0.14000}
MOVED = {"latitude": 51.60000, "longitude": -0.30000}
PORTRAIT = {"width": 390, "height": 844}


@pytest.fixture(scope="module")
def gps_devices(instance, device_harness, tmp_path_factory):
    """The labels of two GPS Devices on the shared instance: (heading north, standing still)."""
    token = secrets.token_hex(2)
    labels = []
    for name, epochs in (("north", NORTH), ("still", STILL)):
        tracks = write_track(tmp_path_factory.mktemp(f"tracks-{name}"), track_lines(epochs), f"{name}.nmea")
        device = device_harness.add_device(f"mapview-{name}-{token}", gps=True, gps_tracks=tracks,
                                           settings={"GPS_SOURCE": f"replay:{name}.nmea"})
        instance.add_device(device)
        labels.append(device.label)
    yield labels
    with instance.client() as api:
        for label in labels:
            api.delete(f"/api/devices/{label}")
    for label in labels:
        device_harness.remove_device(device_harness.devices[label])  # each encodes 720p in software


@pytest.fixture
def fixed_device(instance, test_pattern_device, unique_label):
    """The label of a Device with no GPS Sidecar, added with no Position yet."""
    label = unique_label("mapfixed")
    instance.add_device(test_pattern_device, label=label)
    yield label
    with instance.client() as api:
        api.delete(f"/api/devices/{label}")


def switch(page: Page):
    return page.get_by_role("banner").get_by_role("group", name="View")


def maplibre_fetched(page: Page) -> bool:
    return page.evaluate(
        "() => performance.getEntriesByType('resource').some((entry) => entry.name.includes('maplibre-gl'))")


def on_map(page: Page, label: str) -> bool:
    """Whether the marker's middle is inside the map, clear of the edge by a few pixels."""
    area = map_view(page).locator(".map").bounding_box()
    dot = marker(page, label).bounding_box()
    x, y = dot["x"] + dot["width"] / 2, dot["y"] + dot["height"] / 2
    return area["x"] + 4 <= x <= area["x"] + area["width"] - 4 and area["y"] + 4 <= y <= area["y"] + area["height"] - 4


def screen_position(page: Page, label: str) -> tuple[float, float]:
    dot = marker(page, label).bounding_box()
    return dot["x"], dot["y"]


def test_the_switch_opens_map_view_with_a_marker_per_device_and_a_reload_keeps_it(offline, instance, gps_devices):
    page = offline.new_page()
    page.goto(instance.base_url)
    multi = page.get_by_role("region", name="Multi View")
    expect(multi).to_be_visible()
    expect(switch(page).get_by_role("button", name="Multi View")).to_have_attribute("aria-pressed", "true")
    expect(switch(page).get_by_role("button", name="Map View")).to_have_attribute("aria-pressed", "false")
    page.wait_for_timeout(1_000)  # long enough for `positions` to have arrived
    assert not maplibre_fetched(page)  # Multi View alone never loads the map library

    switch(page).get_by_role("button", name="Map View").click()

    expect(map_view(page)).to_be_visible()
    expect(multi).to_be_hidden()
    expect(switch(page).get_by_role("button", name="Map View")).to_have_attribute("aria-pressed", "true")
    expect(switch(page).get_by_role("button", name="Multi View")).to_have_attribute("aria-pressed", "false")
    for label in gps_devices:
        expect(marker(page, label)).to_be_visible(timeout=20_000)
    expect(marker(page, gps_devices[0])).to_have_accessible_name(re.compile(r": 51\.5\d+, -0\.12346, heading 0°, Feed Status \w+$"))
    assert maplibre_fetched(page)

    page.reload()

    expect(map_view(page)).to_be_visible()
    expect(switch(page).get_by_role("button", name="Map View")).to_have_attribute("aria-pressed", "true")
    for label in gps_devices:
        expect(marker(page, label)).to_be_visible(timeout=20_000)

    switch(page).get_by_role("button", name="Multi View").click()

    expect(multi).to_be_visible()
    expect(map_view(page)).to_be_hidden()
    assert offline.blocked == []


def test_on_a_phone_the_switch_stays_in_the_masthead_and_map_view_fills_the_screen(offline, instance, gps_devices):
    page = offline.new_page()
    page.set_viewport_size(PORTRAIT)
    page.goto(instance.base_url)
    button = switch(page).get_by_role("button", name="Map View")
    expect(button).to_be_in_viewport()
    box = button.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= PORTRAIT["width"]

    button.click()

    expect(map_view(page)).to_be_visible()
    expect(marker(page, gps_devices[0])).to_be_visible(timeout=20_000)
    area = map_view(page).locator(".map").bounding_box()
    assert area["width"] >= PORTRAIT["width"] - 2 * 24
    assert area["height"] >= PORTRAIT["height"] * 0.6  # the viewing area under the masthead
    assert offline.blocked == []


def test_the_map_fits_every_marker_and_refits_as_a_device_joins_and_moves(
        offline, instance, gps_devices, fixed_device):
    page = offline.new_page()
    open_map_view(page, instance, gps_devices)
    page.wait_for_timeout(300)  # let MapLibre draw the frame
    assert all(on_map(page, label) for label in gps_devices)

    place(instance, fixed_device, JOINED)

    expect(marker(page, fixed_device)).to_be_visible(timeout=10_000)
    page.wait_for_timeout(300)
    assert all(on_map(page, label) for label in [*gps_devices, fixed_device])

    place(instance, fixed_device, MOVED)  # some 13 km off, far outside the last fit

    expect(marker(page, fixed_device)).to_have_accessible_name(re.compile(r": 51\.60000, -0\.30000, Fixed Position, Feed Status \w+$"))
    page.wait_for_timeout(300)
    assert all(on_map(page, label) for label in [*gps_devices, fixed_device])
    assert offline.blocked == []


def spread(page: Page, labels) -> float:
    """How far apart two markers are on screen, in pixels."""
    (ax, ay), (bx, by) = (screen_position(page, label) for label in labels)
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def latitude(page: Page, label: str) -> float:
    return float(re.search(r": (-?\d+\.\d+),", marker(page, label).get_attribute("aria-label")).group(1))


def test_a_zoom_with_two_devices_in_view_sticks_across_fixes_until_a_marker_leaves_the_view(
        offline, instance, gps_devices):
    page = offline.new_page()
    open_map_view(page, instance, gps_devices)
    north = gps_devices[0]
    # Wait for the north Device's track to start again south of the still one, so for the next
    # 20 s or so it moves into the fitted box rather than out of it, and leaves the view only when
    # the test zooms it out.
    deadline = time.monotonic() + 70
    while latitude(page, north) > 51.5012:
        assert time.monotonic() < deadline
        page.wait_for_timeout(250)
    page.wait_for_timeout(1_500)  # the refit its jump back south made has drawn
    fitted = spread(page, gps_devices)

    area = map_view(page).locator(".map").bounding_box()
    page.mouse.move(area["x"] + area["width"] / 2, area["y"] + area["height"] / 2)
    page.mouse.wheel(0, -60)  # about 7% closer, so both markers stay clear of the edges
    page.wait_for_timeout(1_000)  # the wheel's zoom eases in

    zoomed = spread(page, gps_devices)
    assert zoomed > 1.03 * fitted, (fitted, zoomed)
    assert all(on_map(page, label) for label in gps_devices)
    page.wait_for_timeout(3_000)  # three fixes or more, since Map View asks for one a second
    assert spread(page, gps_devices) > 1.03 * fitted, (fitted, zoomed, spread(page, gps_devices))
    expect(map_view(page).get_by_role("button", name="Recentre")).to_be_hidden()  # still following

    zoom_in = map_view(page).get_by_role("button", name="Zoom in")
    zoom_in.click()
    zoom_in.click()  # four times as close: both markers are out of view

    deadline = time.monotonic() + 5
    while not all(on_map(page, label) for label in gps_devices):  # the next fix refits
        assert time.monotonic() < deadline, "the map never refitted"
        page.wait_for_timeout(250)
    assert spread(page, gps_devices) < 1.5 * fitted
    assert offline.blocked == []


def test_a_drag_stops_the_refit_and_recentre_fits_every_marker_again(offline, instance, gps_devices, fixed_device):
    page = offline.new_page()
    open_map_view(page, instance, gps_devices)
    recentre = map_view(page).get_by_role("button", name="Recentre")
    expect(recentre).to_be_hidden()

    area = map_view(page).locator(".map").bounding_box()
    x, y = area["x"] + area["width"] / 2, area["y"] + area["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x - 200, y - 150, steps=8)
    page.mouse.move(x - 400, y - 300, steps=8)
    page.mouse.up()

    expect(recentre).to_be_visible()
    page.wait_for_timeout(300)
    left = screen_position(page, gps_devices[1])  # the Device standing still
    place(instance, fixed_device, MOVED)
    expect(marker(page, fixed_device)).to_be_attached(timeout=10_000)
    page.wait_for_timeout(300)
    after = screen_position(page, gps_devices[1])
    assert abs(after[0] - left[0]) < 2 and abs(after[1] - left[1]) < 2  # the map stayed where it was left
    assert not on_map(page, fixed_device)

    recentre.click()

    expect(recentre).to_be_hidden()
    page.wait_for_timeout(300)
    assert all(on_map(page, label) for label in [*gps_devices, fixed_device])
    assert offline.blocked == []


def test_panning_with_the_arrow_keys_stops_the_refit_too(offline, instance, gps_devices):
    page = offline.new_page()
    open_map_view(page, instance, gps_devices)
    recentre = map_view(page).get_by_role("button", name="Recentre")
    expect(recentre).to_be_hidden()

    map_view(page).locator("canvas").focus()
    page.keyboard.press("ArrowLeft")

    expect(recentre).to_be_visible()
    assert offline.blocked == []


def test_choosing_a_marker_opens_its_map_preview_and_single_view_from_map_view_returns_there(
        offline, instance, gps_devices):
    page = offline.new_page()
    label = gps_devices[0]
    open_map_view(page, instance, gps_devices)

    marker(page, label).click()

    preview = page.get_by_role("region", name=re.compile(rf"^Map Preview: {label} · "))
    expect(preview).to_be_visible()
    page.get_by_role("button", name="Management Bar").click()  # Single View, from the Camera list
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(rf"\b{label}\b")).click()
    close_bar(page)  # it lies over the back button

    single = page.get_by_role("region", name="Single View")
    expect(single).to_be_visible()
    expect(map_view(page)).to_be_hidden()
    expect(single.get_by_role("heading", name=re.compile(label))).to_be_visible()
    back = single.get_by_role("button", name="← Map View")
    expect(back).to_be_visible()

    page.reload()  # the View Single View came from is view state too

    expect(back).to_be_visible()

    back.click()

    expect(map_view(page)).to_be_visible()
    expect(single).to_be_hidden()
    expect(marker(page, label)).to_be_visible(timeout=20_000)
    expect(preview).to_be_hidden()  # never kept across a reload
    assert offline.blocked == []


def test_by_keyboard_a_marker_opens_its_map_preview_esc_closes_it_and_esc_leaves_single_view_for_map_view(
        offline, instance, gps_devices):
    page = offline.new_page()
    label = gps_devices[1]
    page.goto(instance.base_url)
    switch(page).get_by_role("button", name="Map View").click()
    bar = page.get_by_role("complementary", name="Management Bar")
    expect(bar).to_be_visible()  # seeded open
    preview = page.get_by_role("region", name=re.compile(rf"^Map Preview: {label} · "))

    marker(page, label).focus()
    page.keyboard.press("Enter")

    expect(preview).to_be_visible()

    page.keyboard.press("Escape")

    expect(bar).to_be_hidden()  # the open bar takes the first Esc
    expect(preview).to_be_visible()

    page.keyboard.press("Escape")

    expect(preview).to_be_hidden()
    expect(map_view(page)).to_be_visible()

    page.get_by_role("button", name="Management Bar").click()
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(rf"\b{label}\b")).press("Enter")
    single = page.get_by_role("region", name="Single View")
    expect(single).to_be_visible()
    close_bar(page)

    page.keyboard.press("Escape")

    expect(single).to_be_hidden()
    expect(map_view(page)).to_be_visible()
    assert offline.blocked == []


def test_ticking_connect_in_map_view_stays_in_map_view_and_puts_the_camera_in_multi_view(
        offline, instance, gps_devices):
    page = offline.new_page()
    label = gps_devices[0]
    page.goto(instance.base_url)
    switch(page).get_by_role("button", name="Map View").click()
    expect(marker(page, label)).to_be_visible(timeout=20_000)
    multi = page.get_by_role("region", name="Multi View")

    page.get_by_role("list", name="Cameras").get_by_role("checkbox", name=re.compile(rf"\b{label}\b")).check()

    page.wait_for_timeout(300)
    expect(map_view(page)).to_be_visible()
    expect(multi).to_be_hidden()
    expect(switch(page).get_by_role("button", name="Map View")).to_have_attribute("aria-pressed", "true")

    switch(page).get_by_role("button", name="Multi View").click()

    expect(multi.get_by_role("article", name=re.compile(rf"\b{label}\b"))).to_be_visible()
    assert offline.blocked == []


def test_single_view_opened_from_multi_view_still_returns_to_multi_view(offline, instance, gps_devices):
    page = offline.new_page()
    label = gps_devices[0]
    open_map_view(page, instance, gps_devices)
    switch(page).get_by_role("button", name="Multi View").click()
    page.get_by_role("button", name="Management Bar").click()  # reopen it to reach the Camera list

    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(rf"\b{label}\b")).click()

    single = page.get_by_role("region", name="Single View")
    expect(single).to_be_visible()
    expect(single.get_by_role("button", name="← Multi View")).to_be_visible()
    close_bar(page)

    page.keyboard.press("Escape")

    expect(page.get_by_role("region", name="Multi View")).to_be_visible()
    expect(map_view(page)).to_be_hidden()
    assert offline.blocked == []
