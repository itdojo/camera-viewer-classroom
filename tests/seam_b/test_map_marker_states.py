"""Seam B: Map View marker states (#80), offline on the blank basemap.

A marker shows the worst Feed Status among its Device's Cameras (Offline, Down, Degraded, Idle,
Live, worst first) in its colour, its shape and at the end of its name. A stale marker is greyed
and named "last known" with its age, a Fixed Position is drawn square and named so, and Devices
with no Position are listed over the map. Names are what the tests read, as everywhere on the
map; colours are read from the marker in the light theme and compared with the design tokens.

Degraded is left to the evaluator's own tests: it needs a throttled uplink to fill a Device's
write queue (`test_device_throttle.py`), and the marker draws it by the same rule as the others.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from harness.gps import Epoch, track_lines, write_track
from harness.map_view import address_of
from harness.offline import Offline

HERE = {"latitude": 51.52000, "longitude": -0.14000}
THERE = {"latitude": 51.53000, "longitude": -0.15000}
TRACK = "still.nmea"
EPOCH = Epoch("093005.00", 51.50001, -0.12346, 35.4, 5.0, 47.0)
STALE_SECONDS = 10  # the shared instance's default, once Position Demand is renewed

# Light-theme token values, as getComputedStyle reports them.
JADE = "rgb(60, 184, 88)"
DANGER = "rgb(168, 51, 74)"
GREY = "rgb(107, 101, 130)"  # --gray-500

BODY_STYLE = """(marker) => {
    const style = getComputedStyle(marker.querySelector('.map-marker-arrow'));
    return { colour: style.backgroundColor, radius: style.borderRadius, ring: getComputedStyle(marker).outlineStyle };
}"""


def map_view(page: Page):
    return page.get_by_role("region", name="Map View")


def marker(page: Page, label: str):
    return map_view(page).get_by_role("button", name=re.compile(rf"^{label}(, \S+)?: "))


def body(page: Page, label: str) -> dict:
    """The colour and corner radius of the marker's dot or arrow, and the style of its ring."""
    return marker(page, label).evaluate(BODY_STYLE)


def open_map_view(page: Page, instance) -> None:
    page.emulate_media(color_scheme="light")
    page.goto(instance.base_url)
    page.get_by_role("banner").get_by_role("group", name="View").get_by_role("button", name="Map View").click()
    page.get_by_role("button", name="Management Bar").click()  # it lies over the left of the map
    expect(page.get_by_role("complementary", name="Management Bar")).to_be_hidden()


def place(instance, label: str, where: dict) -> None:
    with instance.client() as api:
        assert api.put(f"/api/devices/{label}/fixed-position", json=where).status_code == 200


def test_a_marker_shows_the_worst_feed_status_of_its_devices_cameras(offline, instance, device_harness, unique_label):
    """Its own kit Device, since the test stops it. The Fixed Position puts it on the map at once."""
    device = device_harness.add_device(unique_label("worst"))
    label = device.label
    instance.add_device(device)
    recordings = []
    try:
        place(instance, label, HERE)
        page = offline.new_page()
        open_map_view(page, instance)
        status = marker(page, label).locator(".map-marker-status")

        expect(marker(page, label)).to_have_accessible_name(
            f"{label}, {address_of(instance, label)}: 51.52000, -0.14000, Fixed Position, Feed Status Idle",
            timeout=20_000)
        assert body(page, label)["radius"] != "50%"  # a Fixed Position is square, a fix round
        expect(status).to_be_visible()  # every status but Live carries a glyph, not colour alone

        with instance.client() as api:  # a Recording pulls the Feed, so it goes Live
            started = api.post("/api/recordings", json={"camera": f"{label}/test-pattern"})
            assert started.status_code == 201, started.text
            recordings.append(started.json()["id"])

        expect(marker(page, label)).to_have_accessible_name(re.compile(r", Feed Status Live$"), timeout=30_000)
        assert body(page, label)["colour"] == JADE
        expect(status).to_be_hidden()

        with instance.client() as api:  # a second Camera on the Device, on a path it doesn't have
            joined = api.post("/api/cameras", json={"device": label, "name": "missing",
                                                    "url": device.rtsps_url("missing", credentials=False)})
            assert joined.status_code == 201, joined.text
            started = api.post("/api/recordings", json={"camera": joined.json()["id"]})
            assert started.status_code == 201, started.text
            recordings.append(started.json()["id"])

        # One Camera Live and one Down: the marker shows the worse.
        expect(marker(page, label)).to_have_accessible_name(re.compile(r", Feed Status Down$"), timeout=30_000)
        assert body(page, label)["colour"] == DANGER
        expect(status).to_be_visible()

        device.stop()

        expect(marker(page, label)).to_have_accessible_name(re.compile(r", Feed Status Offline$"), timeout=30_000)
        expect(status).to_be_visible()
        assert offline.blocked == []
    finally:
        with instance.client() as api:
            for recording in recordings:
                api.post(f"/api/recordings/{recording}/stop")
            api.delete(f"/api/devices/{label}")
        device_harness.remove_device(device)


@pytest.fixture
def gps_device(instance, device_harness, unique_label, tmp_path):
    tracks = write_track(tmp_path / "tracks", track_lines([EPOCH]), TRACK)
    device = device_harness.add_device(unique_label("stale"), gps=True, gps_tracks=tracks,
                                       settings={"GPS_SOURCE": f"replay:{TRACK}"})
    instance.add_device(device)
    yield device
    with instance.client() as api:
        api.delete(f"/api/devices/{device.label}")


def test_a_stale_marker_is_greyed_and_named_last_known_with_its_age(offline, instance, gps_device):
    label = gps_device.label
    page = offline.new_page()
    open_map_view(page, instance)

    expect(marker(page, label)).to_have_accessible_name(
        f"{label}, {address_of(instance, label)}: 51.50001, -0.12346, heading 47°, Feed Status Idle", timeout=20_000)
    assert body(page, label)["ring"] == "none"

    # Renew Position Demand so a 1 Hz fix arrives and the 10 s threshold applies (#64).
    with instance.client() as api:
        assert api.put(f"/api/devices/{label}/position-demand").status_code == 204
    page.wait_for_timeout(2_000)
    gps_device.stop_gps()

    expect(marker(page, label)).to_have_accessible_name(
        re.compile(rf"^{label}, \S+: 51\.50001, -0\.12346, heading 47°, last known 1\d s ago, Feed Status Idle$"),
        timeout=(STALE_SECONDS + 5) * 1000)
    expect(marker(page, label)).to_have_attribute("data-stale", "")
    assert body(page, label)["colour"] == GREY
    assert body(page, label)["ring"] == "dashed"  # what tells it from an Idle fix
    expect(marker(page, label)).to_have_accessible_name(re.compile(r"last known (1[2-9]|2\d) s ago"), timeout=5_000)
    assert offline.blocked == []


@pytest.fixture(scope="module")
def unplaced(instances):
    """An instance of its own, so no other test's Device shows up in its No Position list."""
    started = instances.start("map-no-position")
    yield started
    instances.remove(started)


def test_devices_with_no_position_are_listed_over_the_map_as_positions_come_and_go(
        browser, unplaced, test_pattern_device, unique_label):
    first, second = unique_label("nopos-a"), unique_label("nopos-b")
    guard = Offline(browser.new_context(), unplaced)
    page = guard.new_page()
    open_map_view(page, unplaced)
    listing = map_view(page).get_by_role("status")

    unplaced.add_device(test_pattern_device, label=first)

    expect(listing).to_have_text(f"No Position: {first}", timeout=10_000)

    unplaced.add_device(test_pattern_device, label=second)

    expect(listing).to_have_text(f"No Position: {first}, {second}")

    place(unplaced, first, HERE)

    expect(marker(page, first)).to_be_visible(timeout=10_000)
    expect(listing).to_have_text(f"No Position: {second}")

    place(unplaced, second, THERE)

    expect(marker(page, second)).to_be_visible(timeout=10_000)
    expect(map_view(page).locator(".map-no-position")).to_be_hidden()  # every Device has one

    with unplaced.client() as api:
        assert api.delete(f"/api/devices/{first}/fixed-position").status_code == 204

    expect(listing).to_have_text(f"No Position: {first}", timeout=10_000)
    expect(marker(page, first)).to_have_count(0)
    assert guard.blocked == []
    guard.close()
