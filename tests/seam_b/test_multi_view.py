"""Seam B: Multi View — the Cameras a browser puts on screen, in a grid sized
to the selection, with a Single View round trip that keeps the selection."""

from __future__ import annotations

import re
import secrets

import pytest
from playwright.sync_api import Page, expect

# Nine Cameras, the most Multi View holds, spread over two test-pattern
# Devices: each Device is added to the instance under several labels, so
# every label is its own Camera with its own Relay path.
CAMERAS = 9


@pytest.fixture(scope="module")
def on_screen_labels(instance, test_pattern_device, device_harness):
    second = device_harness.add_device("tp-multi")
    token = secrets.token_hex(2)
    labels = []
    for n in range(CAMERAS):
        label = f"grid-{token}-{n + 1}"
        instance.add_device(test_pattern_device if n % 2 == 0 else second, label=label)
        labels.append(label)
    yield labels
    device_harness.remove_device(second)  # its 720p encode is not free


def put_on_screen(page: Page, label: str) -> None:
    page.get_by_role("list", name="Cameras").get_by_role(
        "checkbox", name=re.compile(rf"\b{label}\b")).check()


def multi_view(page: Page):
    return page.get_by_role("region", name="Multi View")


def tiles(page: Page):
    return multi_view(page).get_by_role("article")


GRID = """() => {
    const boxes = [...document.querySelectorAll('[aria-label="Multi View"] article')]
        .map(tile => tile.getBoundingClientRect());
    const distinct = values => new Set(values.map(Math.round)).size;
    const viewer = document.querySelector('[aria-label="Multi View"]').getBoundingClientRect();
    return {
        columns: distinct(boxes.map(box => box.left)),
        rows: distinct(boxes.map(box => box.top)),
        widths: distinct(boxes.map(box => box.width)),
        inside: boxes.every(box => box.left >= viewer.left - 1 && box.right <= viewer.right + 1),
    };
}"""

TILES_PLAYING = """(count) => {
    const videos = [...document.querySelectorAll('[aria-label="Multi View"] article video')];
    return videos.length === count && videos.every(v => v.videoWidth > 0 && v.currentTime > 0.5);
}"""


@pytest.mark.parametrize("count, columns, rows", [(1, 1, 1), (2, 2, 1), (4, 2, 2), (6, 3, 2), (9, 3, 3)])
def test_multi_view_sizes_its_grid_to_the_selection(page, instance, on_screen_labels, count, columns, rows):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(instance.base_url)
    for label in on_screen_labels[:count]:
        put_on_screen(page, label)

    expect(tiles(page)).to_have_count(count)
    grid = page.evaluate(GRID)
    assert (grid["columns"], grid["rows"]) == (columns, rows)
    assert grid["widths"] == 1, "every tile in a grid is the same size"
    assert grid["inside"], "the grid fits the viewing area"
    page.wait_for_function(TILES_PLAYING, arg=count, timeout=30_000)

    for n, label in enumerate(on_screen_labels[:count]):
        tile = tiles(page).nth(n)
        expect(tile).to_contain_text(label)          # its Device
        expect(tile).to_contain_text("test-pattern")  # the Camera name
        expect(tile).to_contain_text("1280×720")      # its resolution


def test_a_phone_on_its_side_puts_three_cameras_in_one_row_that_fits_the_screen(page, instance, on_screen_labels):
    page.set_viewport_size({"width": 844, "height": 390})
    page.goto(instance.base_url)
    for label in on_screen_labels[:3]:
        put_on_screen(page, label)

    expect(tiles(page)).to_have_count(3)
    grid = page.evaluate(GRID)
    assert (grid["columns"], grid["rows"]) == (3, 1)
    assert grid["inside"]
    bottom = page.evaluate("""() => Math.max(...[...document.querySelectorAll('[aria-label="Multi View"] article')]
        .map(tile => tile.getBoundingClientRect().bottom))""")
    assert bottom <= 390, "every tile, bar and controls included, is on screen without scrolling"
    for n in range(3):  # nothing in a tile's bar is cut off
        record = tiles(page).nth(n).get_by_role("button", name=re.compile("Record"))
        expect(record).to_be_in_viewport(ratio=1)


def test_nine_cameras_on_a_phone_on_its_side_scroll_rather_than_crush_the_video(page, instance, on_screen_labels):
    page.set_viewport_size({"width": 844, "height": 390})
    page.goto(instance.base_url)
    for label in on_screen_labels[:9]:
        put_on_screen(page, label)

    expect(tiles(page)).to_have_count(9)
    sizes = page.evaluate("""() => [...document.querySelectorAll('[aria-label="Multi View"] article')].map(tile => {
        const box = tile.getBoundingClientRect(), video = tile.querySelector('video').getBoundingClientRect();
        const bar = tile.querySelector('.tile-bar').getBoundingClientRect();
        return { width: box.width, video: video.height, clipped: bar.bottom > box.bottom + 1 };
    })""")
    for size in sizes:
        assert size["width"] >= 14 * 16 - 1, size  # the 14rem floor
        assert size["video"] >= 4.5 * 16 - 2, size  # the 4.5rem floor, less the border
        assert not size["clipped"], size


SINGLE_VIEW_PLAYING = """() => {
    const video = document.querySelector('[aria-label="Single View"] video');
    return video && video.videoWidth > 0 && video.currentTime > 0.5;
}"""


def test_clicking_a_tile_opens_single_view_and_esc_returns_to_the_same_multi_view(
        page, instance, on_screen_labels):
    page.goto(instance.base_url)
    chosen = on_screen_labels[:4]
    for label in chosen:
        put_on_screen(page, label)
    page.wait_for_function(TILES_PLAYING, arg=4, timeout=30_000)
    # Close the (harness-seeded) Management Bar: while it is open, the first
    # Esc closes it instead of leaving Single View (test_management_bar.py).
    page.get_by_role("button", name="Management Bar").click()

    tiles(page).nth(2).click()
    single = page.get_by_role("region", name="Single View")
    expect(single).to_be_visible()
    expect(multi_view(page)).to_be_hidden()
    expect(single.get_by_role("heading")).to_contain_text(chosen[2])
    page.wait_for_function(SINGLE_VIEW_PLAYING, timeout=20_000)

    page.keyboard.press("Escape")
    expect(single).to_be_hidden()
    expect(multi_view(page)).to_be_visible()
    expect(tiles(page)).to_have_count(4)
    for n, label in enumerate(chosen):
        expect(tiles(page).nth(n)).to_contain_text(label)
    assert page.evaluate(GRID)["columns"] == 2
    page.wait_for_function(TILES_PLAYING, arg=4, timeout=30_000)


def test_each_browser_holds_its_own_selection_and_a_reload_keeps_it(browser, instance, on_screen_labels):
    first = browser.new_context().new_page()
    second = browser.new_context().new_page()
    for page in (first, second):
        page.goto(instance.base_url)
    for label in on_screen_labels[:2]:
        put_on_screen(first, label)
    put_on_screen(second, on_screen_labels[5])

    expect(tiles(first)).to_have_count(2)
    expect(tiles(second)).to_have_count(1)
    expect(tiles(second).first).to_contain_text(on_screen_labels[5])

    first.reload()
    expect(tiles(first)).to_have_count(2)
    for n, label in enumerate(on_screen_labels[:2]):
        expect(tiles(first).nth(n)).to_contain_text(label)
    checked = first.get_by_role("list", name="Cameras").get_by_role("checkbox", checked=True)
    expect(checked).to_have_count(2)
    expect(tiles(second)).to_have_count(1)  # untouched by the other browser
    first.wait_for_function(TILES_PLAYING, arg=2, timeout=30_000)
    first.context.close()
    second.context.close()


def test_multi_view_holds_at_most_nine_cameras(page, instance, test_pattern_device, on_screen_labels, unique_label):
    tenth = unique_label("tenth")
    instance.add_device(test_pattern_device, label=tenth)
    page.goto(instance.base_url)
    for label in on_screen_labels:
        put_on_screen(page, label)

    cameras = page.get_by_role("list", name="Cameras")
    expect(cameras.get_by_role("checkbox", name=re.compile(tenth))).to_be_disabled()
    # Taking one off frees a place.
    cameras.get_by_role("checkbox", name=re.compile(rf"\b{on_screen_labels[0]}\b")).uncheck()
    put_on_screen(page, tenth)
    expect(tiles(page)).to_have_count(9)
    expect(tiles(page).last).to_contain_text(tenth)
