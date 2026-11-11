"""Seam B: the Map Preview (#89, #91; Back and the Single View round trip, #92), offline on the blank basemap.

Choosing a marker in Map View opens a panel docked on the map's right edge that plays the
Device's first Camera at once over WHEP, with the Camera's Feed Status, Stall overlay,
Unencrypted and recording badges. Markers here are the shared test-pattern Device under new
labels, given a Fixed Position so they appear at once with no wait for a fix.

"One WHEP session at a time" is read from the browser's own peer connections: an init script
counts every RTCPeerConnection the page opens and closes, in order.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from harness.map_view import EAST, HERE, close_bar, map_view, marker, open_map_view, place

# Every RTCPeerConnection the page opens and closes, in order, and the most open at once.
COUNT_PEER_CONNECTIONS = """(() => {
    const Native = window.RTCPeerConnection;
    const log = window.peerConnections = { open: 0, most: 0, events: [] };
    window.RTCPeerConnection = class extends Native {
        constructor(...args) {
            super(...args);
            this.counted = true;
            log.open += 1;
            log.most = Math.max(log.most, log.open);
            log.events.push('open');
        }
        close() {
            if (this.counted) {
                this.counted = false;
                log.open -= 1;
                log.events.push('close');
            }
            super.close();
        }
    };
})();"""

PREVIEW_PLAYING = """() => {
    const video = document.querySelector('[aria-label^="Map Preview"] video');
    return Boolean(video && video.videoWidth > 0 && video.currentTime > 1);
}"""


def preview(page: Page):
    return page.get_by_role("region", name=re.compile(r"^Map Preview"))


def test_choosing_a_marker_plays_its_devices_camera_in_the_map_preview_without_a_further_click(
        offline, instance, placed):
    label = placed("preview")
    page = offline.new_page()
    open_map_view(page, instance, [label])

    marker(page, label).click()

    panel = page.get_by_role("region", name=f"Map Preview: {label} · test-pattern", exact=True)
    expect(panel).to_be_visible()
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    expect(panel.get_by_role("heading", name=f"{label} · test-pattern")).to_be_visible()
    expect(panel.get_by_text("Live", exact=True)).to_be_visible(timeout=10_000)  # its Feed Status
    expect(map_view(page).locator(".map")).to_be_visible()  # beside the map, not instead of it
    expect(page.locator(".maplibregl-popup")).to_have_count(0)  # no Camera popup any more
    assert offline.blocked == []


def peer_connections(page: Page) -> dict:
    return page.evaluate("() => window.peerConnections")


def test_choosing_another_marker_swaps_the_preview_closing_the_old_session_before_opening_the_new(
        offline, instance, placed):
    first, second = placed("swap-a", HERE), placed("swap-b", EAST)
    offline.context.add_init_script(COUNT_PEER_CONNECTIONS)
    page = offline.new_page()
    open_map_view(page, instance, [first, second])

    marker(page, first).click()
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {first} · test-pattern")
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    assert peer_connections(page)["events"] == ["open"]  # the counter sees the preview's session

    marker(page, second).click()

    expect(preview(page)).to_have_accessible_name(f"Map Preview: {second} · test-pattern")
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    expect(preview(page)).to_have_count(1)
    log = peer_connections(page)
    assert log["events"] == ["open", "close", "open"], "the old session closes before the new one opens"
    assert (log["most"], log["open"]) == (1, 1)
    assert offline.blocked == []


def hold_whep_offers(page: Page) -> list:
    """Hold every WHEP offer the page sends, unanswered, until the test lets it go with
    `route.fallback()`; returns the list the held routes are appended to."""
    held = []
    page.route("**/api/feeds/**/whep", lambda route: held.append(route))
    return held


def wait_until_held(page: Page, held: list, count: int) -> None:
    for _ in range(200):
        if len(held) >= count:
            return
        page.wait_for_timeout(50)
    raise AssertionError(f"{len(held)} WHEP offers held, expected {count}")


def test_swapping_before_the_old_feed_answers_still_leaves_at_most_one_peer_connection(
        offline, instance, placed):
    first, second = placed("race-a", HERE), placed("race-b", EAST)
    offline.context.add_init_script(COUNT_PEER_CONNECTIONS)
    page = offline.new_page()
    held = hold_whep_offers(page)
    open_map_view(page, instance, [first, second])

    marker(page, first).click()
    wait_until_held(page, held, 1)  # the first Feed's offer is out, its answer not yet back
    marker(page, second).click()
    wait_until_held(page, held, 2)

    log = peer_connections(page)
    assert log["events"] == ["open", "close", "open"], "the unanswered session closes at once"
    assert (log["most"], log["open"]) == (1, 1)

    with page.expect_request(lambda request: request.method == "DELETE"
                             and f"/api/feeds/{first}" in request.url) as deleted:
        for route in held:
            route.fallback()  # the late answer arrives, and its session is ended on the Relay too
    assert deleted.value.url.split("/whep/")[1]
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {second} · test-pattern")
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    log = peer_connections(page)
    assert log["events"] == ["open", "close", "open"]
    assert (log["most"], log["open"]) == (1, 1)
    assert offline.blocked == []


def add_second_camera(instance, label: str, test_pattern_device) -> None:
    """A second Camera on the Device: the same test pattern by its own URL."""
    with instance.client() as api:
        joined = api.post("/api/cameras", json={"device": label, "name": "second",
                                                "url": test_pattern_device.rtsps_url(credentials=False)})
        assert joined.status_code == 201, joined.text


def camera_choices(page: Page):
    return preview(page).get_by_role("list", name="Camera choices", exact=True)


def test_only_a_device_with_two_cameras_lists_them_in_camera_order_with_feed_status_and_the_playing_one_current(
        offline, instance, placed, test_pattern_device):
    lone, label = placed("one-cam", HERE), placed("two-cams", EAST)
    add_second_camera(instance, label, test_pattern_device)
    page = offline.new_page()
    open_map_view(page, instance, [lone, label])

    marker(page, lone).click()
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {lone} · test-pattern")
    expect(camera_choices(page)).to_have_count(0)

    marker(page, label).click()
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · test-pattern")
    choices = camera_choices(page).get_by_role("button")
    expect(choices).to_have_text([re.compile(r"^test-pattern"), re.compile(r"^second")])
    expect(choices.nth(0)).to_have_attribute("aria-current", "true")
    expect(choices.nth(1)).not_to_have_attribute("aria-current", "true")
    expect(choices.nth(0).locator(".feed-status")).to_have_text("Live", timeout=20_000)
    expect(choices.nth(1).locator(".feed-status")).to_have_text(re.compile(r"^(Idle|Live)$"))

    page.get_by_role("button", name="Management Bar").click()
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(rf"^second {label}\b")).focus()
    page.keyboard.press("Alt+ArrowUp")  # "second" moves above the Device's kit Camera

    expect(choices).to_have_text([re.compile(r"^second"), re.compile(r"^test-pattern")])
    expect(choices.nth(1)).to_have_attribute("aria-current", "true")  # reordering switches nothing
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · test-pattern")
    assert offline.blocked == []


def test_choosing_another_camera_switches_the_preview_and_never_holds_two_sessions_even_before_an_answer(
        offline, instance, placed, test_pattern_device):
    label = placed("switch")
    add_second_camera(instance, label, test_pattern_device)
    offline.context.add_init_script(COUNT_PEER_CONNECTIONS)
    page = offline.new_page()
    open_map_view(page, instance, [label])
    marker(page, label).click()
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    choices = camera_choices(page).get_by_role("button")

    choices.filter(has_text="second").click()

    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · second")
    expect(choices.nth(1)).to_have_attribute("aria-current", "true")
    expect(choices.nth(0)).not_to_have_attribute("aria-current", "true")
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    expect(choices.nth(1).locator(".feed-status")).to_have_text("Live", timeout=20_000)
    log = peer_connections(page)
    assert log["events"] == ["open", "close", "open"], "the old session closes before the new one opens"
    assert (log["most"], log["open"]) == (1, 1)

    held = hold_whep_offers(page)
    choices.filter(has_text="test-pattern").click()
    wait_until_held(page, held, 1)  # its offer is out, its answer not yet back
    choices.filter(has_text="second").click()
    wait_until_held(page, held, 2)

    log = peer_connections(page)
    assert log["events"] == ["open", "close"] * 3 + ["open"], "the unanswered session closes at once"
    assert (log["most"], log["open"]) == (1, 1)
    for route in held:
        route.fallback()
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · second")
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    assert peer_connections(page)["open"] == 1
    assert offline.blocked == []


def test_the_camera_that_plays_is_the_devices_first_in_this_browsers_camera_order(
        offline, instance, placed, test_pattern_device):
    label = placed("order")
    with instance.client() as api:  # a second Camera on the Device: the same test pattern by its own URL
        joined = api.post("/api/cameras", json={"device": label, "name": "second",
                                                "url": test_pattern_device.rtsps_url(credentials=False)})
        assert joined.status_code == 201, joined.text
    page = offline.new_page()
    open_map_view(page, instance, [label])
    marker(page, label).click()
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · test-pattern")  # the list's first
    page.keyboard.press("Escape")
    expect(preview(page)).to_be_hidden()

    page.get_by_role("button", name="Management Bar").click()
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(rf"^second {label}\b")).focus()
    page.keyboard.press("Alt+ArrowUp")  # "second" moves above the Device's kit Camera
    close_bar(page)
    marker(page, label).click()

    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · second")
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    assert offline.blocked == []


def test_a_feed_that_never_starts_says_so_with_its_feed_status_and_an_unencrypted_camera_is_marked(
        offline, instance, unique_label):
    label = unique_label("never")
    with instance.client() as api:  # plain RTSP to an address nothing answers on
        assert api.post("/api/cameras", json={"device": label, "url": "rtsp://192.0.2.10:8554/cam"}).status_code == 201
    try:
        place(instance, label, HERE)
        page = offline.new_page()
        open_map_view(page, instance, [label])

        marker(page, label).click()

        panel = preview(page)
        expect(panel).to_be_visible()
        expect(panel.get_by_text(re.compile(r"^Feed unavailable: .*Retrying…$"))).to_be_visible(timeout=30_000)
        expect(panel.locator(".feed-status")).to_have_text(re.compile(r"^Down"), timeout=30_000)
        badge = panel.get_by_text("Unencrypted", exact=True)
        expect(badge).to_be_visible()
        expect(badge).to_have_attribute("title", re.compile("plain RTSP"))
        assert offline.blocked == []
    finally:
        with instance.client() as api:
            api.delete(f"/api/devices/{label}")


def test_the_preview_shows_its_cameras_recording_badge_and_a_kit_camera_has_no_unencrypted_badge(
        offline, instance, placed):
    label = placed("recorded")
    page = offline.new_page()
    open_map_view(page, instance, [label])
    marker(page, label).click()
    panel = preview(page)
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    expect(panel.get_by_text("Recording", exact=True)).to_have_count(0)
    expect(panel.get_by_text("Unencrypted", exact=True)).to_have_count(0)

    with instance.client() as api:
        started = api.post("/api/recordings", json={"camera": f"{label}/test-pattern"})
        assert started.status_code == 201, started.text
    try:
        expect(panel.get_by_text("Recording", exact=True)).to_be_visible(timeout=20_000)
    finally:
        with instance.client() as api:
            api.post(f"/api/recordings/{started.json()['id']}/stop")
    assert offline.blocked == []


def test_esc_after_the_management_bar_the_close_button_and_leaving_map_view_close_the_preview_and_its_session(
        offline, instance, placed):
    label = placed("closing")
    offline.context.add_init_script(COUNT_PEER_CONNECTIONS)
    page = offline.new_page()
    open_map_view(page, instance, [label], bar_open=True)
    bar = page.get_by_role("complementary", name="Management Bar")
    panel = preview(page)

    marker(page, label).focus()
    page.keyboard.press("Enter")
    expect(panel).to_be_visible()
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    page.keyboard.press("Escape")
    expect(bar).to_be_hidden()  # the open bar takes the first Esc
    expect(panel).to_be_visible()
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    page.wait_for_function("() => window.peerConnections.open === 0")

    marker(page, label).focus()
    page.keyboard.press(" ")
    expect(panel).to_be_visible()
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    panel.get_by_role("button", name="Close Map Preview").click()
    expect(panel).to_be_hidden()
    page.wait_for_function("() => window.peerConnections.open === 0")

    marker(page, label).click()
    expect(panel).to_be_visible()
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    page.get_by_role("banner").get_by_role("group", name="View").get_by_role("button", name="Multi View").click()
    expect(panel).to_be_hidden()
    page.wait_for_function("() => window.peerConnections.open === 0")
    page.get_by_role("banner").get_by_role("group", name="View").get_by_role("button", name="Map View").click()
    expect(marker(page, label)).to_be_visible(timeout=20_000)
    expect(panel).to_be_hidden()  # coming back opens nothing by itself
    assert peer_connections(page)["events"] == ["open", "close"] * 3
    assert offline.blocked == []


def test_clicking_or_dragging_the_empty_map_leaves_the_preview_open_and_playing(offline, instance, placed):
    label = placed("stays")
    page = offline.new_page()
    open_map_view(page, instance, [label])
    marker(page, label).click()
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)

    area = map_view(page).locator(".map").bounding_box()
    x, y = area["x"] + 80, area["y"] + area["height"] * 0.7
    page.mouse.click(x, y)
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 60, y - 40, steps=6)
    page.mouse.up()

    page.wait_for_timeout(500)
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · test-pattern")
    page.wait_for_function(PREVIEW_PLAYING, timeout=5_000)
    assert offline.blocked == []


def test_removing_the_previewed_device_closes_the_preview_and_its_session(offline, instance, placed):
    label = placed("removed")
    offline.context.add_init_script(COUNT_PEER_CONNECTIONS)
    page = offline.new_page()
    open_map_view(page, instance, [label])
    marker(page, label).click()
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)

    with instance.client() as api:
        assert api.delete(f"/api/devices/{label}").status_code in (200, 204)

    expect(preview(page)).to_be_hidden(timeout=10_000)
    expect(marker(page, label)).to_have_count(0)
    page.wait_for_function("() => window.peerConnections.open === 0")
    assert offline.blocked == []


def test_a_device_with_no_cameras_opens_the_preview_with_no_cameras_and_no_video(
        offline, instance, instance_image, device_harness, unique_label):
    label = unique_label("bare")
    device = device_harness.add_scripted_device(label, instance_image, paths=[])
    instance.add_device(device, label=label)
    try:
        place(instance, label, HERE)
        page = offline.new_page()
        open_map_view(page, instance, [label])

        marker(page, label).click()

        panel = page.get_by_role("region", name=f"Map Preview: {label}", exact=True)
        expect(panel).to_be_visible()
        expect(panel.get_by_text("No Cameras", exact=True)).to_be_visible()
        expect(panel.locator("video")).to_be_hidden()
        assert offline.blocked == []
    finally:
        with instance.client() as api:
            api.delete(f"/api/devices/{label}")
        device_harness.remove_scripted_device(device)


def beside_the_preview(page: Page, label: str) -> bool:
    """Whether the marker's middle is on the part of the map the preview leaves, clear of the
    edges by a few pixels."""
    area = map_view(page).locator(".map").bounding_box()
    panel = preview(page).bounding_box()
    dot = marker(page, label).bounding_box()
    x, y = dot["x"] + dot["width"] / 2, dot["y"] + dot["height"] / 2
    return area["x"] + 4 <= x <= panel["x"] - 4 and area["y"] + 4 <= y <= area["y"] + area["height"] - 4


def test_follow_and_recentre_fit_the_markers_into_the_part_of_the_map_the_preview_leaves(
        offline, instance, placed):
    west, east = placed("fit-w", HERE), placed("fit-e", EAST)
    page = offline.new_page()
    open_map_view(page, instance, [west, east])
    page.wait_for_timeout(300)
    panel = preview(page)

    marker(page, east).click()

    expect(panel).to_be_visible()
    page.wait_for_timeout(300)
    assert all(beside_the_preview(page, label) for label in (west, east))
    box = panel.bounding_box()
    for control in ("Zoom in", "Zoom out"):  # MapLibre's controls stay clear of the panel
        assert map_view(page).get_by_role("button", name=control).bounding_box()["x"] + 20 <= box["x"]

    area = map_view(page).locator(".map").bounding_box()
    x, y = area["x"] + 200, area["y"] + area["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 200, y + 150, steps=8)
    page.mouse.move(x + 400, y + 300, steps=8)
    page.mouse.up()
    recentre = map_view(page).get_by_role("button", name="Recentre")
    expect(recentre).to_be_visible()
    page.wait_for_timeout(1_000)  # MapLibre's inertia
    assert not all(beside_the_preview(page, label) for label in (west, east))

    recentre.click()

    expect(recentre).to_be_hidden()
    page.wait_for_timeout(300)
    assert all(beside_the_preview(page, label) for label in (west, east))
    assert offline.blocked == []


def test_a_lone_followed_marker_is_centred_in_the_part_of_the_map_the_preview_leaves(offline, instance, placed):
    label = placed("lone")
    page = offline.new_page()
    open_map_view(page, instance, [label])
    area = map_view(page).locator(".map").bounding_box()

    marker(page, label).click()

    expect(preview(page)).to_be_visible()
    page.wait_for_timeout(300)
    panel = preview(page).bounding_box()
    dot = marker(page, label).bounding_box()
    middle = dot["x"] + dot["width"] / 2
    assert abs(middle - (area["x"] + (panel["x"] - area["x"]) / 2)) < 40  # not under the panel's edge
    assert offline.blocked == []


def history_length(page: Page) -> int:
    return page.evaluate("() => history.length")


def test_back_closes_the_preview_leaving_map_view_and_swapping_markers_adds_no_history_entries(
        offline, instance, placed):
    first, second = placed("back-a", HERE), placed("back-b", EAST)
    offline.context.add_init_script(COUNT_PEER_CONNECTIONS)
    page = offline.new_page()
    open_map_view(page, instance, [first, second])
    app, entries = page.url, history_length(page)

    marker(page, first).click()
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {first} · test-pattern")
    marker(page, second).click()
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {second} · test-pattern")
    marker(page, first).click()
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {first} · test-pattern")
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    assert history_length(page) == entries + 1, "one entry for the open preview, however often it swaps"

    page.go_back()

    expect(preview(page)).to_be_hidden()
    assert page.url == app
    expect(map_view(page)).to_be_visible()
    expect(marker(page, first)).to_be_visible()
    page.wait_for_function("() => window.peerConnections.open === 0")
    assert offline.blocked == []


def single_view(page: Page):
    return page.get_by_role("region", name="Single View")


def open_in_single_view(page: Page, label: str) -> None:
    preview(page).get_by_role("button", name="Open in Single View").click()
    expect(single_view(page).get_by_role("heading", name=f"test-pattern · {label}")).to_be_visible()
    expect(map_view(page)).to_be_hidden()


def leave_single_view(page: Page, way: str) -> None:
    if way == "back button":
        single_view(page).get_by_role("button", name="← Map View").click()
    elif way == "Esc":
        page.keyboard.press("Escape")
    else:
        page.get_by_role("banner").get_by_role("group", name="View").get_by_role("button", name="Map View").click()


@pytest.mark.parametrize("way", ["back button", "Esc", "masthead switch"])
def test_every_way_back_from_single_view_opened_from_the_preview_reopens_the_preview(
        offline, instance, placed, way):
    label = placed("return")
    offline.context.add_init_script(COUNT_PEER_CONNECTIONS)
    page = offline.new_page()
    open_map_view(page, instance, [label])
    marker(page, label).click()
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    entries = history_length(page)

    open_in_single_view(page, label)
    expect(preview(page)).to_be_hidden()
    leave_single_view(page, way)

    expect(map_view(page)).to_be_visible()
    expect(single_view(page)).to_be_hidden()
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · test-pattern")
    expect(preview(page).get_by_role("button", name="Close Map Preview")).to_be_focused()  # as any opening (#94)
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    page.wait_for_function("() => window.peerConnections.open === 1")  # Single View's session closed
    assert history_length(page) == entries, "the round trip leaves one entry for the preview"
    page.go_back()
    expect(preview(page)).to_be_hidden()
    expect(map_view(page)).to_be_visible()
    assert offline.blocked == []


def test_the_reopened_preview_plays_the_camera_single_view_showed_when_left_on_that_cameras_device(
        offline, instance, placed):
    first, second = placed("left-a", HERE), placed("left-b", EAST)
    page = offline.new_page()
    open_map_view(page, instance, [first, second])
    marker(page, first).click()
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    open_in_single_view(page, first)

    page.get_by_role("button", name="Management Bar").click()  # another Camera, from the Camera list
    cameras = page.get_by_role("list", name="Cameras")
    cameras.get_by_role("button", name=re.compile(rf"^test-pattern {second}\b")).click()
    close_bar(page)
    expect(single_view(page).get_by_role("heading", name=f"test-pattern · {second}")).to_be_visible()
    leave_single_view(page, "back button")

    expect(preview(page)).to_have_accessible_name(f"Map Preview: {second} · test-pattern")
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    assert offline.blocked == []


def test_a_reload_comes_back_to_map_view_with_no_preview_and_no_whep_session(offline, instance, placed):
    label = placed("reload")
    offline.context.add_init_script(COUNT_PEER_CONNECTIONS)
    page = offline.new_page()
    open_map_view(page, instance, [label])
    marker(page, label).click()
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    assert peer_connections(page)["events"][0] == "open"  # the counter sees the preview's session

    page.reload()

    expect(marker(page, label)).to_be_visible(timeout=20_000)
    page.wait_for_timeout(2_000)  # time enough for a Feed to have been asked for
    expect(preview(page)).to_be_hidden()
    assert peer_connections(page)["events"] == []
    assert offline.blocked == []
