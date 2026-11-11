"""Seam B: the Map Preview by keyboard and screen reader (#94), offline on the blank basemap.

Map View's markers are buttons a viewer reaches with Tab and opens with Enter or Space; each
keeps its Position name and is described as opening the Map Preview. The panel is a region
named after the Device and the Camera playing; opening it moves focus to its close button, and
closing it returns focus to the marker that opened it. Markers are the shared test-pattern
Device under new labels at Fixed Positions, as in `test_map_preview.py`.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect
from test_map_preview import preview

from harness.map_view import EAST, HERE, address_of, map_view, marker, open_map_view


def tab_to(page: Page, target, presses: int = 12) -> None:
    """Press Tab from the map until `target` has focus, failing if it never does."""
    map_view(page).get_by_role("region", name="Map, drag or use the arrow keys to pan", exact=True).focus()
    for _ in range(presses):
        page.keyboard.press("Tab")
        if target.evaluate("(node) => node === document.activeElement"):
            return
    raise AssertionError("Tab never reached the marker")


def test_tab_reaches_a_marker_enter_opens_the_preview_on_its_close_button_and_esc_returns_to_the_marker(
        offline, instance, placed):
    label = placed("keys")
    page = offline.new_page()
    open_map_view(page, instance, [label])
    close = preview(page).get_by_role("button", name="Close Map Preview")

    tab_to(page, marker(page, label))
    page.keyboard.press("Enter")

    expect(preview(page)).to_have_accessible_name(f"Map Preview: {label} · test-pattern")
    expect(close).to_be_focused()

    page.keyboard.press("Escape")

    expect(preview(page)).to_be_hidden()
    expect(marker(page, label)).to_be_focused()
    assert offline.blocked == []


def test_closing_returns_focus_to_the_marker_even_after_a_click_in_the_panel_dropped_it(
        offline, instance, placed):
    label = placed("refocus")
    page = offline.new_page()
    open_map_view(page, instance, [label])
    marker(page, label).click()
    panel = preview(page)
    expect(panel.get_by_role("button", name="Close Map Preview")).to_be_focused()

    panel.get_by_role("heading").click()  # nothing focusable there: focus falls to the page
    expect(panel.get_by_role("button", name="Close Map Preview")).not_to_be_focused()
    page.keyboard.press("Escape")

    expect(panel).to_be_hidden()
    expect(marker(page, label)).to_be_focused()
    assert offline.blocked == []


def test_space_on_another_marker_swaps_the_region_name_to_its_device_and_esc_returns_to_that_marker(
        offline, instance, placed):
    first, second = placed("space-a", HERE), placed("space-b", EAST)
    page = offline.new_page()
    open_map_view(page, instance, [first, second])
    close = preview(page).get_by_role("button", name="Close Map Preview")

    tab_to(page, marker(page, first))
    page.keyboard.press(" ")
    expect(preview(page)).to_have_accessible_name(f"Map Preview: {first} · test-pattern")
    expect(close).to_be_focused()

    tab_to(page, marker(page, second))
    page.keyboard.press(" ")

    expect(preview(page)).to_have_accessible_name(f"Map Preview: {second} · test-pattern")
    expect(close).to_be_focused()
    page.keyboard.press("Enter")  # the close button, by keyboard
    expect(preview(page)).to_be_hidden()
    expect(marker(page, second)).to_be_focused()
    assert offline.blocked == []


def test_a_map_view_marker_is_described_as_opening_the_map_preview_and_keeps_its_position_name(
        offline, instance, placed):
    label = placed("described")
    page = offline.new_page()
    open_map_view(page, instance, [label])

    expect(marker(page, label)).to_have_accessible_name(
        f"{label}, {address_of(instance, label)}: 51.52000, -0.14000, Fixed Position, Feed Status Idle")
    expect(marker(page, label)).to_have_accessible_description("Opens the Map Preview")
    assert offline.blocked == []
