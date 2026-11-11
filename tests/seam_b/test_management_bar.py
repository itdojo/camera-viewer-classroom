"""Seam B: the Management Bar, the slide-over that holds the Camera, Capture
and Device lists and the add forms. It starts closed so the Feeds get the
whole window, and opens over them without re-laying out the Multi View.

These tests start with clean storage: the harness seeds every other browser
context with the bar open (see ``LoggedInBrowser.new_context``)."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from harness.browser import MANAGEMENT_BAR_STORAGE_KEY


@pytest.fixture
def page(browser, instance):
    """A logged-in page with nothing saved, so the bar starts from its defaults."""
    context = browser.new_context(seed_management_bar=False)
    page = context.new_page()
    page.goto(instance.base_url)
    yield page
    if not page.is_closed():
        page.goto("about:blank")  # ends WHEP sessions, as the conftest page does
    context.close()


def toggle(page: Page):
    return page.get_by_role("button", name="Management Bar")


def bar(page: Page):
    return page.get_by_role("complementary", name="Management Bar")


def panel_header(page: Page, name: str):
    """A Panel's header button, which collapses and expands it."""
    return bar(page).get_by_role("button", name=name, exact=True)


def test_the_bar_starts_closed_with_its_contents_out_of_the_accessibility_tree(page):
    expect(toggle(page)).to_have_attribute("aria-expanded", "false")
    expect(bar(page)).to_be_hidden()
    expect(page.get_by_role("heading", name="Cameras", exact=True)).to_have_count(0)
    expect(page.get_by_role("form", name="Add Device")).to_have_count(0)


def test_the_toggle_opens_and_closes_the_bar(page):
    toggle(page).click()
    expect(toggle(page)).to_have_attribute("aria-expanded", "true")
    expect(bar(page)).to_be_visible()
    expect(page.get_by_role("form", name="Add Device")).to_be_visible()

    toggle(page).click()
    expect(toggle(page)).to_have_attribute("aria-expanded", "false")
    expect(bar(page)).to_be_hidden()


def test_backslash_opens_and_closes_the_bar(page):
    page.keyboard.press("\\")
    expect(bar(page)).to_be_visible()
    expect(toggle(page)).to_have_attribute("aria-expanded", "true")

    page.keyboard.press("\\")  # focus is on the first Panel header, not in a text field
    expect(bar(page)).to_be_hidden()
    expect(toggle(page)).to_have_attribute("aria-expanded", "false")


def test_backslash_types_into_a_text_field_instead_of_toggling(page):
    toggle(page).click()
    label = page.get_by_role("form", name="Add Device").get_by_label("Label", exact=True)
    label.click()
    page.keyboard.type("a\\b")

    expect(label).to_have_value("a\\b")
    expect(bar(page)).to_be_visible()


def test_opening_focuses_the_first_panel_header_and_closing_returns_focus_to_the_toggle(page):
    page.keyboard.press("\\")
    expect(panel_header(page, "Cameras")).to_be_focused()

    page.keyboard.press("Escape")
    expect(toggle(page)).to_be_focused()

    toggle(page).click()
    expect(panel_header(page, "Cameras")).to_be_focused()


def test_escape_with_focus_outside_the_bar_closes_it_and_leaves_focus_where_it_was(page):
    toggle(page).click()
    log_out = page.get_by_role("button", name="Log out")
    log_out.focus()

    page.keyboard.press("Escape")
    expect(bar(page)).to_be_hidden()
    expect(log_out).to_be_focused()


PANELS = ["Cameras", "Captures", "Devices", "Add Device"]


def panel_content(page: Page):
    """Something from inside each Panel, by Panel name. The lists may be
    empty, so they are checked by presence in the accessibility tree."""
    return {
        "Cameras": page.get_by_role("list", name="Cameras"),
        "Captures": page.get_by_role("list", name="Captures"),
        "Devices": page.get_by_role("list", name="Devices"),
        "Add Device": page.get_by_role("form", name="Add Device"),
    }


def expect_expanded(page: Page, expanded: set[str]):
    content = panel_content(page)
    for name in PANELS:
        expect(panel_header(page, name)).to_have_attribute("aria-expanded", str(name in expanded).lower())
        expect(content[name]).to_have_count(1 if name in expanded else 0)


def test_every_panel_is_expanded_the_first_time_the_bar_opens(page):
    toggle(page).click()
    expect_expanded(page, set(PANELS))
    expect(page.get_by_role("form", name="Add Device")).to_be_visible()
    expect(page.get_by_role("form", name="Add via RTSP")).to_be_visible()


def test_each_panel_collapses_and_expands_on_its_own(page):
    toggle(page).click()

    panel_header(page, "Devices").click()
    expect_expanded(page, set(PANELS) - {"Devices"})

    panel_header(page, "Add Device").click()
    expect_expanded(page, set(PANELS) - {"Devices", "Add Device"})
    expect(page.get_by_role("form", name="Add via RTSP")).to_have_count(0)  # it sits in Add Device

    panel_header(page, "Devices").click()
    expect_expanded(page, set(PANELS) - {"Add Device"})

    for name in PANELS:
        panel_header(page, name).click()
    expect_expanded(page, {"Add Device"})
    expect(page.get_by_role("form", name="Add via RTSP")).to_be_visible()


def test_which_panels_are_collapsed_survives_a_reload(page):
    toggle(page).click()
    panel_header(page, "Captures").click()
    panel_header(page, "Add Device").click()

    page.reload()
    expect_expanded(page, {"Cameras", "Devices"})

    panel_header(page, "Captures").click()
    page.reload()
    expect_expanded(page, {"Cameras", "Captures", "Devices"})


def test_at_760px_and_under_panels_still_collapse(page):
    page.set_viewport_size({"width": 760, "height": 900})
    panel_header(page, "Add Device").click()
    expect_expanded(page, set(PANELS) - {"Add Device"})

    page.reload()
    expect_expanded(page, set(PANELS) - {"Add Device"})


def expect_order(page: Page, names: list[str]):
    """The Panels as an operator sees them, top to bottom."""
    expect(bar(page).get_by_role("heading", level=2)).to_have_text(names)


def test_alt_arrows_move_the_focused_panel_and_keep_focus_on_it(page):
    toggle(page).click()
    expect_order(page, PANELS)

    panel_header(page, "Cameras").focus()
    page.keyboard.press("Alt+ArrowDown")
    expect_order(page, ["Captures", "Cameras", "Devices", "Add Device"])
    expect(panel_header(page, "Cameras")).to_be_focused()

    page.keyboard.press("Alt+ArrowDown")
    page.keyboard.press("Alt+ArrowDown")
    expect_order(page, ["Captures", "Devices", "Add Device", "Cameras"])
    expect(panel_header(page, "Cameras")).to_be_focused()

    panel_header(page, "Add Device").focus()
    page.keyboard.press("Alt+ArrowUp")
    expect_order(page, ["Captures", "Add Device", "Devices", "Cameras"])
    expect(panel_header(page, "Add Device")).to_be_focused()


def test_a_keyboard_move_announces_the_panels_new_position(page):
    toggle(page).click()
    panel_header(page, "Devices").focus()

    page.keyboard.press("Alt+ArrowUp")
    announcement = bar(page).get_by_role("status")
    expect(announcement).to_have_text("Devices moved to position 2 of 4")

    page.keyboard.press("Alt+ArrowUp")
    expect(announcement).to_have_text("Devices moved to position 1 of 4")


def test_panel_order_survives_a_reload(page):
    toggle(page).click()
    panel_header(page, "Add Device").focus()
    for _ in range(3):
        page.keyboard.press("Alt+ArrowUp")
    moved = ["Add Device", "Cameras", "Captures", "Devices"]
    expect_order(page, moved)

    page.reload()
    expect_order(page, moved)
    toggle(page).click()
    toggle(page).click()  # opening focuses the first Panel header on screen
    expect(panel_header(page, "Add Device")).to_be_focused()


def grip(page: Page, panel: str):
    """A Panel's grip handle, the control that moves it."""
    return bar(page).get_by_role("button", name=f"Move {panel}", exact=True)


def test_alt_arrows_on_a_grip_move_its_panel_and_keep_focus_on_it(page):
    toggle(page).click()
    grip(page, "Devices").focus()

    page.keyboard.press("Alt+ArrowUp")
    expect_order(page, ["Cameras", "Devices", "Captures", "Add Device"])
    expect(grip(page, "Devices")).to_be_focused()
    expect(bar(page).get_by_role("status")).to_have_text("Devices moved to position 2 of 4")

    page.keyboard.press("Alt+ArrowDown")
    expect_order(page, PANELS)
    expect(grip(page, "Devices")).to_be_focused()


def drag_grip(page: Page, panel: str, onto: str):
    """Drag a Panel's grip with the mouse and drop it on another Panel's grip.
    ``hover`` waits for the grip to stop moving, since the bar may still be
    sliding in, and the moves are stepped, as a hand would make them, so the
    pointer events land the same way on every run."""
    target = grip(page, onto)
    grip(page, panel).hover()
    end = target.bounding_box()
    page.mouse.down()
    page.mouse.move(end["x"] + end["width"] / 2, end["y"] + end["height"] / 2, steps=12)
    page.mouse.up()


def test_dragging_a_grip_moves_its_panel_and_the_order_survives_a_reload(page):
    toggle(page).click()
    expect_order(page, PANELS)

    # Dropped on Add Device's header, Cameras lands just above it.
    drag_grip(page, "Cameras", onto="Add Device")
    moved = ["Captures", "Devices", "Cameras", "Add Device"]
    expect_order(page, moved)
    expect(bar(page).get_by_role("status")).to_have_text("Cameras moved to position 3 of 4")

    page.reload()
    expect_order(page, moved)


def save_layout(page: Page, layout: dict):
    """Leave a layout saved by an older or newer version of the page, then
    load the page over it. Only what is then on screen is asserted."""
    key = json.dumps(MANAGEMENT_BAR_STORAGE_KEY)
    page.context.add_init_script(f"localStorage.setItem({key}, {json.dumps(json.dumps(layout))});")
    page.reload()


def test_a_stale_saved_order_drops_unknown_panels_and_puts_missing_ones_at_their_default_place(page):
    # "recordings" and "add-camera" (now inside Add Device) no longer exist; "captures" and
    # "add-device" were added after it was saved.
    save_layout(page, {"open": True, "order": ["add-camera", "recordings", "devices", "cameras"], "collapsed": []})
    expect_order(page, ["Devices", "Captures", "Cameras", "Add Device"])


def test_with_captures_above_cameras_the_camera_list_still_hides_its_empty_text(
    page, instance, test_pattern_device, unique_label
):
    save_layout(page, {"open": True, "order": ["captures", "cameras"], "collapsed": []})
    expect_order(page, ["Captures", "Cameras", "Devices", "Add Device"])
    label = unique_label("order")
    instance.add_device(test_pattern_device, label=label)

    cameras = page.get_by_role("region", name="Cameras")
    expect(cameras.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(label))).to_be_visible()
    expect(cameras.get_by_text("No Cameras yet")).to_be_hidden()


def test_reset_layout_restores_the_default_order_and_expands_every_panel_and_leaves_the_bar_open(page):
    toggle(page).click()
    panel_header(page, "Add Device").focus()
    page.keyboard.press("Alt+ArrowUp")
    panel_header(page, "Captures").click()
    panel_header(page, "Devices").click()
    expect_order(page, ["Cameras", "Captures", "Add Device", "Devices"])

    bar(page).get_by_role("button", name="Reset layout").click()
    expect_order(page, PANELS)
    expect_expanded(page, set(PANELS))
    expect(bar(page)).to_be_visible()
    expect(toggle(page)).to_have_attribute("aria-expanded", "true")

    page.reload()  # the defaults are what is saved now
    expect_order(page, PANELS)
    expect_expanded(page, set(PANELS))
    expect(bar(page)).to_be_visible()


def test_at_760px_and_under_alt_arrows_still_reorder(page):
    page.set_viewport_size({"width": 760, "height": 900})
    panel_header(page, "Captures").focus()
    page.keyboard.press("Alt+ArrowDown")
    moved = ["Cameras", "Devices", "Captures", "Add Device"]
    expect_order(page, moved)
    expect(panel_header(page, "Captures")).to_be_focused()

    page.reload()
    expect_order(page, moved)


def test_whether_the_bar_is_open_survives_a_reload(page):
    toggle(page).click()
    page.reload()
    expect(bar(page)).to_be_visible()
    expect(toggle(page)).to_have_attribute("aria-expanded", "true")

    toggle(page).click()
    page.reload()
    expect(toggle(page)).to_have_attribute("aria-expanded", "false")
    expect(bar(page)).to_be_hidden()


def test_escape_closes_the_bar(page):
    toggle(page).click()
    page.keyboard.press("Escape")
    expect(bar(page)).to_be_hidden()
    expect(toggle(page)).to_have_attribute("aria-expanded", "false")


@pytest.fixture
def single_view(page, instance, test_pattern_device, unique_label):
    """Open the bar, then a Camera in Single View, and return its region."""
    label = unique_label("bar")
    instance.add_device(test_pattern_device, label=label)
    toggle(page).click()
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(label)).click()
    region = page.get_by_role("region", name="Single View")
    expect(region).to_be_visible()
    return region


def test_opening_the_bar_leaves_the_multi_view_grid_as_it_was(page, instance, test_pattern_device, unique_label):
    label = unique_label("grid")
    instance.add_device(test_pattern_device, label=label)
    toggle(page).click()
    page.get_by_role("list", name="Cameras").get_by_role("checkbox", name=re.compile(label)).check()
    toggle(page).click()
    tile = page.get_by_role("region", name="Multi View").get_by_role("article")
    expect(tile).to_have_count(1)
    expect(bar(page)).to_be_hidden()
    closed = tile.bounding_box()

    toggle(page).click()
    expect(bar(page)).to_be_visible()
    page.wait_for_timeout(300)  # well past the slide-in (--motion-fast), in case it moved anything
    assert tile.bounding_box() == closed


def test_at_760px_and_under_the_bar_is_stacked_and_always_shown(page):
    page.set_viewport_size({"width": 760, "height": 900})
    expect(toggle(page)).to_be_hidden()
    expect(bar(page)).to_be_visible()
    expect(page.get_by_role("form", name="Add Device")).to_be_visible()

    page.keyboard.press("\\")
    expect(bar(page)).to_be_visible()


def test_widening_past_760px_with_the_bar_closed_moves_focus_from_the_bar_to_the_toggle(page):
    page.set_viewport_size({"width": 760, "height": 900})
    panel_header(page, "Devices").focus()

    page.set_viewport_size({"width": 1280, "height": 720})
    expect(bar(page)).to_be_hidden()
    expect(toggle(page)).to_be_focused()


def test_under_reduced_motion_widening_past_760px_still_moves_focus_from_the_bar_to_the_toggle(page):
    # No slide holds the closing bar visible, so focus has already dropped to <body> by the time
    # the media query reports the change.
    page.emulate_media(reduced_motion="reduce")
    page.set_viewport_size({"width": 760, "height": 900})
    panel_header(page, "Devices").focus()

    page.set_viewport_size({"width": 1280, "height": 720})
    expect(bar(page)).to_be_hidden()
    expect(toggle(page)).to_be_focused()


def test_in_single_view_the_first_escape_closes_the_bar_and_the_second_leaves(page, single_view):
    expect(bar(page)).to_be_visible()  # clicking a Camera leaves the bar open

    page.keyboard.press("Escape")
    expect(bar(page)).to_be_hidden()
    expect(single_view).to_be_visible()

    page.keyboard.press("Escape")
    expect(single_view).to_be_hidden()
    expect(page.get_by_role("region", name="Multi View")).to_be_visible()


ALERT = re.compile("certificate alert")


def test_a_certificate_alert_badges_the_collapsed_devices_panel_and_the_closed_toggle_without_opening_either(
        page, instances, device_harness, unique_label):
    # Its own instance, so no other test's rebuilt Device adds to the count.
    own = instances.start(unique_label("badge"))
    device = device_harness.add_device(unique_label("badged"))
    try:
        own.add_device(device)
        page.goto(own.base_url)
        toggle(page).click()
        devices = panel_header(page, "Devices")
        devices.click()
        toggle(page).click()
        expect(toggle(page)).not_to_have_accessible_description(ALERT)

        device.regenerate_cert()  # a weekly rebuild raises the certificate alert

        # The count is visible beside the toggle and is the toggle's description.
        expect(toggle(page)).to_have_accessible_description("1 certificate alert", timeout=30_000)
        expect(page.get_by_role("banner").get_by_text("1 certificate alert")).to_be_visible()
        expect(toggle(page)).to_have_attribute("aria-expanded", "false")
        expect(bar(page)).to_be_hidden()

        toggle(page).click()
        expect(toggle(page)).not_to_have_accessible_description(ALERT)
        expect(devices).to_have_attribute("aria-expanded", "false")
        expect(devices).to_have_accessible_description("1 certificate alert")
        devices_panel = bar(page).get_by_role("region", name="Devices", exact=True)
        expect(devices_panel.get_by_text("1 certificate alert")).to_be_visible()
        # The badge leaves the heading and region names alone.
        expect(page.get_by_role("heading", name="Devices", exact=True)).to_be_visible()

        devices.click()
        expect(devices).not_to_have_accessible_description(ALERT)
        expect(devices_panel.get_by_role("region", name=re.compile(f"{device.label}.*certificate changed"))).to_be_visible()
    finally:
        device_harness.remove_device(device)
        instances.remove(own)


def test_in_single_view_with_the_bar_closed_escape_returns_to_multi_view(page, single_view):
    toggle(page).click()
    expect(bar(page)).to_be_hidden()

    page.keyboard.press("Escape")
    expect(single_view).to_be_hidden()
    expect(page.get_by_role("region", name="Multi View")).to_be_visible()
