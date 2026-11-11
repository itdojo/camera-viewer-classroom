"""Seam B: the Camera and Device lists reorder like the Panels, by Alt+↑/↓ or by dragging an
entry's grip; the order belongs to this browser, survives a reload, and Multi View's tiles follow
the Camera list's order rather than the order Cameras were ticked.

Each test adds the shared test-pattern Device under two labels of its own, one after the other,
so its two Cameras (and Devices) sit next to each other at the end of the lists.
"""

import re

import pytest
from playwright.sync_api import Page, expect


@pytest.fixture
def two_labels(instance, test_pattern_device, unique_label):
    labels = [unique_label("order-a"), unique_label("order-b")]
    for label in labels:
        instance.add_device(test_pattern_device, label=label)
    yield labels
    with instance.client() as api:
        for label in labels:
            api.delete(f"/api/devices/{label}")


def named(*labels: str):
    """Text holding any of these labels. No word boundary: an entry's text runs the Camera name
    into its label ("test-patternorder-a-…"), and each label is unique anyway."""
    return re.compile("|".join(map(re.escape, labels)))


def camera_items(page: Page, *labels: str):
    """These labels' entries in the Camera list, top to bottom."""
    return page.get_by_role("list", name="Cameras").get_by_role("listitem").filter(has_text=named(*labels))


def device_items(page: Page, *labels: str):
    """These labels' entries in the Device list, top to bottom."""
    return page.get_by_role("list", name="Devices").get_by_role("listitem").filter(
        has=page.locator(".device-label").filter(has_text=named(*labels)))


def tiles(page: Page):
    return page.get_by_role("region", name="Multi View").get_by_role("article")


def announcement(page: Page):
    return page.get_by_role("complementary", name="Management Bar").get_by_role("status")


def in_order(*labels: str):
    return [named(label) for label in labels]


def test_alt_arrows_move_a_camera_and_multi_view_follows_the_list_and_a_reload_keeps_it(page, instance, two_labels):
    first, second = two_labels
    page.goto(instance.base_url)
    cameras = page.get_by_role("list", name="Cameras")
    for label in two_labels:
        cameras.get_by_role("checkbox", name=named(label)).check()
    expect(tiles(page)).to_have_text(in_order(first, second))

    button = cameras.get_by_role("button", name=named(second))
    button.focus()
    page.keyboard.press("Alt+ArrowUp")

    expect(camera_items(page, *two_labels)).to_have_text(in_order(second, first))
    expect(tiles(page)).to_have_text(in_order(second, first))  # not the order they were ticked
    expect(button).to_be_focused()
    expect(announcement(page)).to_have_text(re.compile(rf"^test-pattern · {second} moved to position \d+ of \d+$"))

    page.reload()
    expect(camera_items(page, *two_labels)).to_have_text(in_order(second, first))
    expect(tiles(page)).to_have_text(in_order(second, first))


def test_a_camera_ticked_after_a_move_still_takes_its_place_in_the_list_order(page, instance, two_labels):
    first, second = two_labels
    page.goto(instance.base_url)
    cameras = page.get_by_role("list", name="Cameras")
    cameras.get_by_role("button", name=named(second)).focus()
    page.keyboard.press("Alt+ArrowUp")
    expect(camera_items(page, *two_labels)).to_have_text(in_order(second, first))

    cameras.get_by_role("checkbox", name=named(first)).check()
    cameras.get_by_role("checkbox", name=named(second)).check()
    expect(tiles(page)).to_have_text(in_order(second, first))


def test_dragging_a_cameras_grip_moves_it(page, instance, two_labels):
    first, second = two_labels
    page.goto(instance.base_url)
    grip = camera_items(page, first).get_by_role("button", name="Move", exact=True)
    target = camera_items(page, second)
    expect(target).to_be_visible()

    grip.hover()  # waits for the bar to stop sliding in
    box = target.bounding_box()
    page.mouse.down()
    # Below the other Camera's middle, so it lands after it.
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] * 0.9, steps=12)
    page.mouse.up()

    expect(camera_items(page, *two_labels)).to_have_text(in_order(second, first))


def test_alt_arrows_on_a_devices_grip_move_it_and_a_reload_keeps_it(page, instance, two_labels):
    first, second = two_labels
    page.goto(instance.base_url)
    expect(device_items(page, *two_labels)).to_have_text(in_order(first, second))

    grip = device_items(page, second).get_by_role("button", name="Move", exact=True)
    grip.focus()
    page.keyboard.press("Alt+ArrowUp")

    expect(device_items(page, *two_labels)).to_have_text(in_order(second, first))
    expect(grip).to_be_focused()
    expect(announcement(page)).to_have_text(re.compile(rf"^{second} moved to position \d+ of \d+$"))

    page.reload()
    expect(device_items(page, *two_labels)).to_have_text(in_order(second, first))


def test_each_browser_keeps_its_own_order(browser, instance, two_labels):
    first, second = two_labels
    mover = browser.new_context().new_page()
    other = browser.new_context().new_page()
    for page in (mover, other):
        page.goto(instance.base_url)
    mover.get_by_role("list", name="Cameras").get_by_role("button", name=named(second)).focus()
    mover.keyboard.press("Alt+ArrowUp")
    expect(camera_items(mover, *two_labels)).to_have_text(in_order(second, first))

    other.reload()
    expect(camera_items(other, *two_labels)).to_have_text(in_order(first, second))
    mover.context.close()
    other.context.close()
