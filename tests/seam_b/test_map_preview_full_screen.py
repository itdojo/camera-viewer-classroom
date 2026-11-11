"""Seam B: Full Screen and the phone sheet of the Map Preview (#93, ADR 0013).

The panel's corner button moves its screen node into Full Screen and back, so the Feed keeps
playing on its one WHEP session. Esc leaves Full Screen before it closes the panel. At phone
width the panel is a fixed sheet along the bottom with the map padded above it, and turning the
phone to landscape puts the panel's Feed Full Screen, as in Single View."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect
from test_full_screen import (
    FULL_SCREEN_PLAYING,
    IN_ELEMENT_FULLSCREEN,
    LANDSCAPE,
    NO_ELEMENT_FULLSCREEN,
    PORTRAIT,
    full_screen,
    whep_offers,
)
from test_map_preview import COUNT_PEER_CONNECTIONS, PREVIEW_PLAYING, peer_connections, preview

from harness.map_view import map_view, marker, open_map_view
from harness.offline import Offline


@pytest.fixture
def offline_phone(browser, instance) -> Offline:
    """A phone-sized touch screen that can reach only the shared instance."""
    guard = Offline(browser.new_context(viewport=PORTRAIT, has_touch=True), instance)
    yield guard
    guard.close()


def full_screen_button(page: Page, label: str):
    return preview(page).get_by_role("button", name=f"Full Screen test-pattern · {label}", exact=True)


def test_full_screen_from_the_preview_keeps_its_one_whep_session_and_comes_back_playing(
        offline, instance, placed):
    label = placed("fs-preview")
    offline.context.add_init_script(COUNT_PEER_CONNECTIONS)
    page = offline.new_page()
    open_map_view(page, instance, [label])
    marker(page, label).click()
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    offers = whep_offers(page)

    full_screen_button(page, label).click()

    dialog = full_screen(page)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("heading", name=f"{label} · test-pattern")).to_be_visible()
    page.wait_for_function(FULL_SCREEN_PLAYING, timeout=5_000)
    page.wait_for_function(IN_ELEMENT_FULLSCREEN)  # the exit races the browser's fullscreen otherwise

    dialog.get_by_role("button", name="Exit Full Screen").click()

    expect(dialog).to_be_hidden()
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · test-pattern")
    page.wait_for_function(PREVIEW_PLAYING, timeout=5_000)
    expect(full_screen_button(page, label)).to_be_focused()
    assert offers == [], "Full Screen moved the playing video; it never opened another WHEP session"
    assert peer_connections(page)["events"] == ["open"]
    assert offline.blocked == []


def test_esc_leaves_full_screen_first_and_then_closes_the_preview(offline, instance, placed):
    label = placed("fs-esc")
    offline.context.add_init_script(COUNT_PEER_CONNECTIONS)
    page = offline.new_page()
    open_map_view(page, instance, [label])
    marker(page, label).click()
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    full_screen_button(page, label).click()
    expect(full_screen(page)).to_be_visible()
    page.wait_for_function(IN_ELEMENT_FULLSCREEN)

    page.keyboard.press("Escape")

    expect(full_screen(page)).to_be_hidden()
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · test-pattern")  # one level only
    page.wait_for_function(PREVIEW_PLAYING, timeout=5_000)
    expect(full_screen_button(page, label)).to_be_focused()

    page.keyboard.press("Escape")

    expect(preview(page)).to_be_hidden()
    page.wait_for_function("() => window.peerConnections.open === 0")
    assert peer_connections(page)["events"] == ["open", "close"]
    assert offline.blocked == []


def test_back_leaves_full_screen_and_then_closes_the_preview(offline, instance, placed):
    label = placed("fs-back")
    page = offline.new_page()
    open_map_view(page, instance, [label])
    app = page.url
    marker(page, label).click()
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    full_screen_button(page, label).click()
    expect(full_screen(page)).to_be_visible()
    page.wait_for_function(IN_ELEMENT_FULLSCREEN)

    page.go_back()

    expect(full_screen(page)).to_be_hidden()
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · test-pattern")  # one level only
    page.wait_for_function(PREVIEW_PLAYING, timeout=5_000)

    page.go_back()

    expect(preview(page)).to_be_hidden()
    expect(map_view(page)).to_be_visible()
    assert page.url == app
    assert offline.blocked == []


def test_removing_the_device_while_its_preview_is_full_screen_leaves_no_preview_entry_for_back_to_spend(
        offline, instance, placed):
    label = placed("fs-gone")
    page = offline.new_page()
    open_map_view(page, instance, [label])
    app = page.evaluate("() => history.state")  # the app's own entry, before the preview pushes one
    marker(page, label).click()
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    full_screen_button(page, label).click()
    expect(full_screen(page)).to_be_visible()
    page.wait_for_function(IN_ELEMENT_FULLSCREEN)

    with instance.client() as api:
        assert api.delete(f"/api/devices/{label}").status_code in (200, 204)

    expect(full_screen(page)).to_be_hidden(timeout=10_000)
    expect(preview(page)).to_be_hidden()
    # Back from here leaves Map View's entry, rather than spending itself on a panel already gone.
    page.wait_for_function("(app) => JSON.stringify(history.state) === JSON.stringify(app)", arg=app,
                           timeout=5_000)
    assert offline.blocked == []


def test_on_a_phone_the_preview_is_a_sheet_along_the_bottom_with_the_chosen_marker_above_it(
        offline_phone, instance, placed):
    label = placed("sheet")
    page = offline_phone.new_page()
    open_map_view(page, instance, [label], phone=True)

    marker(page, label).tap()

    panel = preview(page)
    expect(panel).to_be_visible()
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    page.wait_for_timeout(300)  # the map pads around the sheet
    sheet = panel.bounding_box()
    assert (sheet["x"], sheet["width"]) == (0, PORTRAIT["width"]), "it spans the screen's width"
    assert sheet["y"] + sheet["height"] == PORTRAIT["height"], "it sits on the screen's bottom edge"
    assert panel.evaluate("(el) => getComputedStyle(el).position") == "fixed"

    video = panel.locator("video").bounding_box()  # full width, 16:9, below the heading
    assert video["width"] >= sheet["width"] - 40
    assert abs(video["width"] / video["height"] - 16 / 9) < 0.02
    assert panel.get_by_role("heading").bounding_box()["y"] < video["y"]

    area = map_view(page).locator(".map").bounding_box()
    dot = marker(page, label).bounding_box()
    middle = dot["y"] + dot["height"] / 2
    assert area["y"] < dot["y"] and dot["y"] + dot["height"] < sheet["y"], "the marker shows above the sheet"
    assert abs(middle - (area["y"] + (sheet["y"] - area["y"]) / 2)) < 40, "centred in the map the sheet leaves"
    attribution = map_view(page).locator(".maplibregl-ctrl-attrib").bounding_box()
    assert attribution["y"] + attribution["height"] <= sheet["y"]  # MapLibre's controls clear it too

    page.mouse.wheel(0, 2_000)  # scrolling the page leaves the sheet where it is
    page.wait_for_timeout(300)
    assert page.evaluate("window.scrollY") > 0
    assert panel.bounding_box() == sheet
    assert offline_phone.blocked == []


def test_turning_a_phone_puts_the_previews_feed_full_screen_and_turning_back_returns_it(
        offline_phone, instance, placed):
    label = placed("fs-turn")
    offline_phone.context.add_init_script(NO_ELEMENT_FULLSCREEN)  # as on an iPhone; a fullscreen window can't turn
    page = offline_phone.new_page()
    open_map_view(page, instance, [label], phone=True)

    page.set_viewport_size(LANDSCAPE)  # nothing plays yet, so the map stays
    page.wait_for_timeout(300)
    expect(full_screen(page)).to_be_hidden()
    expect(map_view(page)).to_be_visible()
    page.set_viewport_size(PORTRAIT)

    marker(page, label).tap()
    page.wait_for_function(PREVIEW_PLAYING, timeout=20_000)
    offers = whep_offers(page)

    page.set_viewport_size(LANDSCAPE)

    dialog = full_screen(page)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("heading", name=f"{label} · test-pattern")).to_be_visible()
    page.wait_for_function(FULL_SCREEN_PLAYING, timeout=5_000)

    page.set_viewport_size(PORTRAIT)

    expect(dialog).to_be_hidden()
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · test-pattern")
    page.wait_for_function(PREVIEW_PLAYING, timeout=5_000)
    assert offers == [], "the turn moved the playing video; it never opened another WHEP session"

    # Opened by the button, a turn back to portrait leaves it up, as in Single View.
    full_screen_button(page, label).tap()
    page.set_viewport_size(LANDSCAPE)
    page.set_viewport_size(PORTRAIT)
    expect(dialog).to_be_visible()
    assert offline_phone.blocked == []
