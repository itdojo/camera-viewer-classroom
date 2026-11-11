"""Seam B: Marker Details (#96), offline on the blank basemap.

A Map View marker's Marker Detail is a card beside it with its Device's label, address and
Position. It shows while the mouse is over the marker or keyboard focus is on it, and on every
marker while the "Details" control (named "Show all Marker Details") is pressed, which this
browser keeps across a reload. It sits right of its marker, or left where the right would run
past the map or under the Map Preview, and takes no pointer events, so choosing a marker still
opens the Map Preview and nothing else does. A tap is not a hover, so on a phone the toggle is
the only way to see details. The detail is aria-hidden, so tests find it by its class; the
address also joins the marker's name. Markers are the shared test-pattern Device under new
labels at Fixed Positions, as in `test_map_preview.py`.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect
from test_full_screen import PORTRAIT
from test_map_preview import preview
from test_map_preview_keyboard import tab_to

from harness.map_view import EAST, HERE, address_of, map_view, marker, open_map_view
from harness.offline import Offline


@pytest.fixture
def offline_phone(browser, instance) -> Offline:
    """A phone-sized touch screen that can reach only the shared instance."""
    guard = Offline(browser.new_context(viewport=PORTRAIT, has_touch=True), instance)
    yield guard
    guard.close()


def detail(page: Page, label: str):
    """The label's Marker Detail card. Its holder is point-sized at the marker's middle, which
    Playwright would count as hidden, so the card is what shows."""
    return map_view(page).locator(".map-detail", has_text=label).locator(".map-detail-card")


def details_toggle(page: Page):
    return map_view(page).get_by_role("button", name="Show all Marker Details", exact=True)


def middle(box: dict) -> tuple[float, float]:
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def side_of(page: Page, label: str) -> str:
    """Which side of its marker the label's detail card sits on, 'right' or 'left'."""
    dot = marker(page, label).bounding_box()
    card = detail(page, label).bounding_box()
    if card["x"] >= dot["x"] + dot["width"]:
        return "right"
    assert card["x"] + card["width"] <= dot["x"], "the card overlaps its marker"
    return "left"


def away(page: Page) -> None:
    """Move the mouse off every marker, onto the masthead."""
    banner = page.get_by_role("banner").bounding_box()
    page.mouse.move(banner["x"] + 4, banner["y"] + 4)


def test_hovering_a_marker_shows_its_detail_on_its_right_and_leaving_hides_it(offline, instance, placed):
    label = placed("hover")
    address = address_of(instance, label)
    page = offline.new_page()
    open_map_view(page, instance, [label])
    expect(marker(page, label)).to_have_accessible_name(re.compile(
        rf"^{re.escape(label)}, {re.escape(address)}: 51\.52000, -0\.14000, Fixed Position, Feed Status \w+$"))
    expect(detail(page, label)).to_be_hidden()

    marker(page, label).hover()

    card = detail(page, label)
    expect(card).to_be_visible()
    expect(card.locator(".map-detail-label")).to_have_text(label)
    expect(card.locator(".map-detail-address")).to_have_text(address)
    expect(card.locator(".map-detail-coordinates")).to_have_text("51.52000, -0.14000")
    expect(card.locator(".map-detail-note")).to_have_text("Fixed Position")
    expect(map_view(page).locator(".map-detail", has_text=label)).to_have_attribute("aria-hidden", "true")
    assert side_of(page, label) == "right"
    dot_y = middle(marker(page, label).bounding_box())[1]
    card_y = middle(card.bounding_box())[1]
    assert abs(dot_y - card_y) <= 2, "the card is centred on the marker"

    away(page)

    expect(detail(page, label)).to_be_hidden()
    expect(preview(page)).to_be_hidden()  # hovering opens nothing
    assert offline.blocked == []


def test_keyboard_focus_on_a_marker_shows_its_detail_and_enter_still_opens_the_preview(
        offline, instance, placed):
    label = placed("focus")
    page = offline.new_page()
    open_map_view(page, instance, [label])

    tab_to(page, marker(page, label))

    expect(detail(page, label)).to_be_visible()
    page.keyboard.press("Enter")
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · test-pattern")
    expect(detail(page, label)).to_be_hidden()  # focus went to the panel's close button
    assert offline.blocked == []


def test_a_detail_flips_left_at_the_maps_right_edge_and_beside_an_open_preview(offline, instance, placed):
    west = placed("west", HERE)
    east = placed("east", EAST)
    page = offline.new_page()
    open_map_view(page, instance, [west, east])
    details_toggle(page).click()
    expect(detail(page, east)).to_be_visible()

    assert side_of(page, west) == "right"
    assert side_of(page, east) == "left", "the fit leaves the east marker too near the edge for its card"

    marker(page, west).click()
    panel = preview(page)
    expect(panel).to_be_visible()
    page.wait_for_timeout(300)  # the map pads around the panel and refits
    edge = panel.bounding_box()["x"]
    assert middle(marker(page, east).bounding_box())[0] < edge, "the east marker was fitted clear of the panel"
    assert side_of(page, east) == "left"
    card = detail(page, east).bounding_box()
    assert card["x"] + card["width"] <= edge, "the card stays clear of the panel"
    assert offline.blocked == []


def test_the_toggle_shows_every_detail_survives_a_reload_and_opens_nothing(offline, instance, placed):
    west = placed("all-w", HERE)
    east = placed("all-e", EAST)
    page = offline.new_page()
    open_map_view(page, instance, [west, east])
    toggle = details_toggle(page)
    expect(toggle).to_have_attribute("aria-pressed", "false")

    toggle.click()

    expect(toggle).to_have_attribute("aria-pressed", "true")
    for label in (west, east):
        expect(detail(page, label)).to_be_visible()
    expect(preview(page)).to_be_hidden()

    page.reload()
    for label in (west, east):
        expect(marker(page, label)).to_be_visible(timeout=20_000)
    expect(details_toggle(page)).to_have_attribute("aria-pressed", "true")
    for label in (west, east):
        expect(detail(page, label)).to_be_visible()

    details_toggle(page).click()

    expect(details_toggle(page)).to_have_attribute("aria-pressed", "false")
    for label in (west, east):
        expect(detail(page, label)).to_be_hidden()
    marker(page, west).hover()  # hover still works with the toggle off
    expect(detail(page, west)).to_be_visible()
    expect(detail(page, east)).to_be_hidden()
    assert offline.blocked == []


def test_a_click_on_a_detail_reaches_the_map_and_a_marker_click_still_opens_the_preview(
        offline, instance, placed):
    label = placed("through")
    page = offline.new_page()
    open_map_view(page, instance, [label])
    details_toggle(page).click()
    card = detail(page, label)
    expect(card).to_be_visible()

    x, y = middle(card.bounding_box())
    page.mouse.click(x, y)
    expect(preview(page)).to_be_hidden()  # the map took the click, which opens nothing

    page.mouse.move(x, y)  # and a drag from it moves the map
    page.mouse.down()
    page.mouse.move(x - 120, y + 40, steps=8)
    page.mouse.up()
    expect(map_view(page).get_by_role("button", name="Recentre")).to_be_visible()

    marker(page, label).click()
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · test-pattern")
    assert offline.blocked == []


def test_on_a_phone_a_tap_opens_the_preview_with_no_detail_and_the_toggle_shows_them(
        offline_phone, instance, placed):
    label = placed("phone")
    page = offline_phone.new_page()
    open_map_view(page, instance, [label], phone=True)

    marker(page, label).tap()

    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · test-pattern")
    expect(detail(page, label)).to_be_hidden()  # a tap is not a hover
    preview(page).get_by_role("button", name="Close Map Preview").tap()
    expect(preview(page)).to_be_hidden()
    expect(detail(page, label)).to_be_hidden()

    details_toggle(page).tap()

    expect(detail(page, label)).to_be_visible()
    marker(page, label).tap()
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · test-pattern")
    assert offline_phone.blocked == []
