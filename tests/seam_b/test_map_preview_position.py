"""Seam B: the Device's Position in the Map Preview, and opening it from the No Position list
(#90), offline on the blank basemap.

The panel's Position line is the Single View header's (`position.js`), so for the same Position
the two read the same: coordinates (details in a tooltip), speed, heading, the age once stale,
"Fixed Position" for one entered by hand. Its `data-fix` marks live, stale or fixed. A Device
that loses its Position keeps playing and the line reads "No Position". The GPS Device replays
a one-epoch track, so its text is a known literal, as in `test_position_header.py`.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect
from test_map_preview import COUNT_PEER_CONNECTIONS, PREVIEW_PLAYING, peer_connections, preview

from harness.gps import Epoch, track_lines, write_track
from harness.map_view import map_view, marker, open_map_view

TRACK = "still.nmea"
EPOCH = Epoch("093005.00", 51.50001, -0.12346, 35.4, 5.0, 47.0, satellites=9, hdop=0.8)
STALE_SECONDS = 10  # Map View renews Position Demand, so the 1 Hz threshold applies


@pytest.fixture
def gps_device(instance, device_harness, unique_label, tmp_path):
    tracks = write_track(tmp_path / "tracks", track_lines([EPOCH]), TRACK)
    device = device_harness.add_device(unique_label("mpgps"), gps=True, gps_tracks=tracks,
                                       settings={"GPS_SOURCE": f"replay:{TRACK}"})
    instance.add_device(device)
    yield device
    with instance.client() as api:
        api.delete(f"/api/devices/{device.label}")
    device_harness.remove_device(device)  # it encodes 720p in software


def panel_position(page: Page):
    return preview(page).get_by_role("group", name="Position")


def header_position(page: Page, instance, label: str):
    """Open Single View on the Device's Camera from the Camera list; its header's Position."""
    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(rf"\b{label}\b")).click()
    return page.get_by_role("region", name="Single View").locator("header").get_by_role("group", name="Position")


def test_the_preview_shows_the_devices_live_position_as_the_single_view_header_does_and_marks_it_stale(
        offline, instance, gps_device):
    label = gps_device.label
    header_page = offline.new_page()
    header = header_position(header_page, instance, label)
    page = offline.new_page()
    open_map_view(page, instance, [label])

    marker(page, label).click()

    position = panel_position(page)
    expect(position.get_by_text("51.50001, -0.12346", exact=True)).to_be_visible(timeout=15_000)
    expect(position).to_contain_text("18.0 km/h")
    expect(position).to_contain_text("47°")
    expect(position).to_have_attribute("data-fix", "live")
    expect(header).to_be_visible(timeout=15_000)
    assert position.inner_text() == header.inner_text()

    position.get_by_text("51.50001, -0.12346", exact=True).hover()
    expect(page.get_by_role("tooltip")).to_have_text(
        "Altitude 35 m · 9 satellites · HDOP 0.8 · GPS time 2026-10-15 09:30:05 UTC")
    page.mouse.move(0, 0)
    expect(page.get_by_role("tooltip")).to_be_hidden()

    gps_device.stop_gps()

    expect(position).to_have_attribute("data-fix", "stale", timeout=(STALE_SECONDS + 5) * 1000)
    expect(position).to_have_attribute("data-stale", "")
    expect(position.get_by_text("51.50001, -0.12346", exact=True)).to_be_visible()
    expect(position).to_contain_text(re.compile(r"last fix 1\d s ago"))
    expect(position).to_contain_text(re.compile(r"last fix (1[2-9]|2\d) s ago"), timeout=5_000)  # it counts on
    expect(header).to_have_attribute("data-stale", "", timeout=5_000)
    without_age = lambda text: re.sub(r"last fix \d+ s ago", "last fix … ago", text)  # noqa: E731
    assert without_age(position.inner_text()) == without_age(header.inner_text())
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    assert offline.blocked == []


def test_a_fixed_position_is_marked_as_the_header_marks_it_and_losing_it_keeps_the_feed_playing_with_no_position(
        offline, instance, placed):
    label = placed("mpfixed")
    header = header_position(offline.new_page(), instance, label)
    offline.context.add_init_script(COUNT_PEER_CONNECTIONS)
    page = offline.new_page()
    open_map_view(page, instance, [label])
    marker(page, label).click()
    position = panel_position(page)
    expect(position).to_have_attribute("data-fix", "fixed")
    expect(position).to_contain_text("Fixed Position")
    expect(header).to_be_visible(timeout=10_000)
    assert position.inner_text() == header.inner_text() == "51.52000, -0.14000\nFixed Position"
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    before = peer_connections(page)["events"]
    assert before[-1] == "open"

    with instance.client() as api:
        assert api.delete(f"/api/devices/{label}/fixed-position").status_code == 204

    expect(position).to_have_text("No Position", use_inner_text=True, timeout=10_000)
    expect(position).to_have_attribute("data-fix", "none")
    expect(marker(page, label)).to_have_count(0)
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · test-pattern")
    expect(preview(page).get_by_role("button", name="Copy coordinates")).to_be_hidden()
    page.wait_for_timeout(1_000)
    page.wait_for_function(PREVIEW_PLAYING)  # the video has not gone
    assert peer_connections(page)["events"] == before  # the same session, never closed
    assert offline.blocked == []


def test_copy_puts_the_five_decimal_coordinates_on_the_clipboard_and_says_copied_briefly(
        offline, instance, placed):
    label = placed("mpcopy", {"latitude": 51.520004, "longitude": -0.139996})
    offline.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=instance.base_url)
    page = offline.new_page()
    open_map_view(page, instance, [label])
    marker(page, label).click()
    panel = preview(page)

    panel.get_by_role("button", name="Copy coordinates").click()

    expect(panel.get_by_role("status").filter(has_text="Copied")).to_have_text("Copied")
    assert page.evaluate("navigator.clipboard.readText()") == "51.52000, -0.14000"
    expect(panel.get_by_text("Copied", exact=True)).to_be_hidden(timeout=5_000)  # brief
    assert offline.blocked == []


# What a page served over plain HTTP sees: no Clipboard API, since it is not a secure context.
NO_CLIPBOARD = "Object.defineProperty(Navigator.prototype, 'clipboard', { get: () => undefined });"


def test_without_the_clipboard_api_copy_selects_the_coordinates_instead(offline, instance, placed):
    label = placed("mpselect")
    offline.context.add_init_script(NO_CLIPBOARD)
    page = offline.new_page()
    open_map_view(page, instance, [label])
    marker(page, label).click()
    panel = preview(page)

    panel.get_by_role("button", name="Copy coordinates").click()

    assert page.evaluate("String(getSelection())") == "51.52000, -0.14000"
    expect(panel.get_by_text(re.compile(r"^Selected"))).to_be_visible()
    assert offline.blocked == []


def test_a_name_in_the_no_position_list_opens_the_map_preview_for_its_device(
        offline, instance, placed, test_pattern_device, unique_label):
    anchor = placed("mpanchor")  # a marker, so the map opens with something on it
    label = unique_label("mpnopos")
    instance.add_device(test_pattern_device, label=label)
    try:
        page = offline.new_page()
        open_map_view(page, instance, [anchor])
        listing = map_view(page).locator(".map-no-position")
        name = listing.get_by_role("button", name=label, exact=True)
        expect(listing).to_contain_text("No Position: ")  # still one line of text around its buttons

        name.click()

        expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · test-pattern")
        expect(panel_position(page)).to_have_text("No Position", use_inner_text=True)
        page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)

        marker(page, anchor).click()  # then by keyboard, from another Device's preview
        expect(preview(page)).to_have_accessible_name(f"Map Preview: {anchor} · test-pattern")
        name.focus()
        page.keyboard.press("Enter")
        expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · test-pattern")
        assert offline.blocked == []
    finally:
        with instance.client() as api:
            api.delete(f"/api/devices/{label}")
