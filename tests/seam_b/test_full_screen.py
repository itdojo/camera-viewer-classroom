"""Seam B: Full Screen (#68, ADR 0013) on a phone-sized, touch screen. One Feed fills the window
over the view it came from, moved there without a new WHEP session, and every way out puts it
back. Headless Chromium has element fullscreen but draws nothing of it, so the browser's own
fullscreen is seen only through ``document.fullscreenElement``."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

PORTRAIT = {"width": 390, "height": 844}
LANDSCAPE = {"width": 844, "height": 390}

FULL_SCREEN_PLAYING = """() => {
    const video = document.querySelector('dialog.full-screen[open] video');
    if (!video || video.videoWidth === 0) return false;
    const start = video.currentTime;
    return new Promise(resolve => setTimeout(() => resolve(video.currentTime > start), 500));
}"""

TILE_PLAYING = """(label) => {
    const tile = [...document.querySelectorAll('[aria-label="Multi View"] article')]
        .find((t) => t.getAttribute('aria-label').includes(label));
    const video = tile?.querySelector('video');
    return Boolean(video && video.videoWidth > 0 && video.currentTime > 1);
}"""

# A browser without element fullscreen, as on an iPhone: Full Screen is then the overlay alone.
# Headless Chromium has it, and a fullscreen window refuses set_viewport_size.
NO_ELEMENT_FULLSCREEN = "Object.defineProperty(Document.prototype, 'fullscreenEnabled', { get: () => false });"

# The frame has gone fullscreen. Focus can't leave a fullscreen element, so an exit that hands
# focus back must wait for the fullscreen to end.
IN_ELEMENT_FULLSCREEN = "() => document.fullscreenElement?.classList.contains('full-screen-frame')"

SINGLE_PLAYING = """() => {
    const video = document.querySelector('[aria-label="Single View"] video');
    return video && video.videoWidth > 0 && video.currentTime > 1;
}"""


@pytest.fixture
def phone(browser) -> Page:
    page = browser.new_page(viewport=PORTRAIT, has_touch=True)
    yield page
    page.goto("about:blank")  # pagehide ends the WHEP sessions (tests/conftest.py's page)
    page.context.close()


def whep_offers(page: Page) -> list[str]:
    offers: list[str] = []
    page.on("request", lambda r: offers.append(r.url) if r.method == "POST" and r.url.endswith("/whep") else None)
    return offers


def full_screen(page: Page):
    return page.get_by_role("dialog", name="Full Screen")


def tile_of(page: Page, label: str):
    return page.get_by_role("region", name="Multi View").get_by_role("article", name=re.compile(rf"\b{label}\b"))


def on_a_tile(page: Page, instance, label: str):
    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role("checkbox", name=re.compile(rf"\b{label}\b")).check()
    page.wait_for_function(TILE_PLAYING, arg=label, timeout=20_000)
    return tile_of(page, label)


def in_single_view(page: Page, instance, label: str) -> None:
    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(label)).click()
    page.wait_for_function(SINGLE_PLAYING, timeout=20_000)


def test_a_tile_goes_full_screen_in_place_on_the_same_whep_session_and_comes_back(
        phone, instance, test_pattern_device, unique_label):
    label = unique_label("fs-tile")
    instance.add_device(test_pattern_device, label=label)
    tile = on_a_tile(phone, instance, label)
    offers = whep_offers(phone)

    tile.get_by_role("button", name=f"Full Screen test-pattern · {label}").tap()

    dialog = full_screen(phone)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("heading", name=f"{label} · test-pattern")).to_be_visible()
    expect(dialog.get_by_text("Live", exact=True)).to_be_visible()
    phone.wait_for_function(FULL_SCREEN_PLAYING, timeout=5_000)
    box = dialog.bounding_box()
    assert (box["width"], box["height"]) == (PORTRAIT["width"], PORTRAIT["height"])
    expect(phone.get_by_role("region", name="Multi View")).to_be_attached()  # in place, not Single View

    expect(dialog).to_have_attribute("data-chrome", "hidden", timeout=5_000)  # the header goes after 3 s
    dialog.tap(position={"x": 195, "y": 600})
    expect(dialog).not_to_have_attribute("data-chrome", "hidden")  # and a tap brings it back

    dialog.get_by_role("button", name="Exit Full Screen").tap()
    expect(dialog).to_be_hidden()
    phone.wait_for_function(TILE_PLAYING, arg=label, timeout=5_000)
    assert offers == [], "Full Screen moved the playing video; it never opened another WHEP session"
    expect(phone.get_by_role("region", name="Multi View")).to_be_visible()


def test_back_and_esc_close_full_screen_one_level_at_a_time(phone, instance, test_pattern_device, unique_label):
    label = unique_label("fs-exit")
    instance.add_device(test_pattern_device, label=label)
    in_single_view(phone, instance, label)
    stage_button = phone.get_by_role("region", name="Single View").get_by_role(
        "button", name=f"Full Screen test-pattern · {label}")
    app = phone.url

    stage_button.tap()
    expect(full_screen(phone)).to_be_visible()
    phone.wait_for_function(IN_ELEMENT_FULLSCREEN)  # the exit races the browser's fullscreen otherwise
    phone.go_back()
    expect(full_screen(phone)).to_be_hidden()
    assert phone.url == app, "Back closed Full Screen and stayed in the app"
    expect(phone.get_by_role("region", name="Single View")).to_be_visible()
    expect(stage_button).to_be_focused()
    phone.wait_for_function(SINGLE_PLAYING, timeout=5_000)

    stage_button.tap()
    expect(full_screen(phone)).to_be_visible()
    phone.wait_for_function(IN_ELEMENT_FULLSCREEN)
    phone.keyboard.press("Escape")
    expect(full_screen(phone)).to_be_hidden()
    expect(phone.get_by_role("region", name="Single View")).to_be_visible()  # one level only
    expect(stage_button).to_be_focused()
    phone.keyboard.press("Escape")
    expect(phone.get_by_role("region", name="Multi View")).to_be_visible()


def test_turning_a_phone_in_single_view_opens_full_screen_and_turning_back_closes_it(
        phone, instance, test_pattern_device, unique_label):
    label = unique_label("fs-turn")
    instance.add_device(test_pattern_device, label=label)
    phone.add_init_script(NO_ELEMENT_FULLSCREEN)
    in_single_view(phone, instance, label)

    phone.set_viewport_size(LANDSCAPE)
    expect(full_screen(phone)).to_be_visible()
    phone.wait_for_function(FULL_SCREEN_PLAYING, timeout=5_000)
    phone.set_viewport_size(PORTRAIT)
    expect(full_screen(phone)).to_be_hidden()

    # Opened by the button, a turn back to portrait leaves it up.
    phone.get_by_role("region", name="Single View").get_by_role("button", name=re.compile("^Full Screen")).tap()
    phone.set_viewport_size(LANDSCAPE)
    phone.set_viewport_size(PORTRAIT)
    expect(full_screen(phone)).to_be_visible()


def test_turning_a_phone_in_multi_view_leaves_the_grid(phone, instance, test_pattern_device, unique_label):
    label = unique_label("fs-grid")
    instance.add_device(test_pattern_device, label=label)
    on_a_tile(phone, instance, label)

    phone.set_viewport_size(LANDSCAPE)
    expect(tile_of(phone, label)).to_be_visible()
    expect(full_screen(phone)).to_be_hidden()


def test_full_screen_closes_when_its_camera_goes_away(phone, instance, test_pattern_device, unique_label):
    label = unique_label("fs-gone")
    instance.add_device(test_pattern_device, label=label)
    tile = on_a_tile(phone, instance, label)
    tile.get_by_role("button", name=re.compile("^Full Screen")).tap()
    expect(full_screen(phone)).to_be_visible()

    with instance.client() as api:
        api.delete(f"/api/devices/{label}")

    expect(full_screen(phone)).to_be_hidden()
    expect(tile_of(phone, label)).to_have_count(0)
    expect(phone.get_by_role("region", name="Multi View")).to_be_visible()


def test_the_header_stays_while_the_feed_is_not_live(phone, instance, device_harness, unique_label):
    device = device_harness.add_device(unique_label("fs-down"))
    instance.add_device(device)
    device.regenerate_cert()  # the Relay now refuses it, so the Feed never comes Live
    phone.goto(instance.base_url)
    phone.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(device.label)).click()

    phone.get_by_role("region", name="Single View").get_by_role("button", name=re.compile("^Full Screen")).tap()
    dialog = full_screen(phone)
    expect(dialog).to_be_visible()
    phone.wait_for_timeout(4_000)  # past the 3 s a Live Feed's header waits
    expect(dialog).not_to_have_attribute("data-chrome", "hidden")
    expect(dialog.get_by_role("button", name="Exit Full Screen")).to_be_visible()


# Records what the page's wake lock requests came to, calling through to the browser's own.
WATCH_WAKE_LOCK = """
window.__wakeLocks = [];
if (navigator.wakeLock) {
    const request = navigator.wakeLock.request.bind(navigator.wakeLock);
    navigator.wakeLock.request = (type) => request(type).then(
        (sentinel) => { window.__wakeLocks.push({ type, held: true, sentinel }); return sentinel; },
        (error) => { window.__wakeLocks.push({ type, held: false, error: error.name }); throw error; });
}
"""


def test_full_screen_keeps_the_screen_awake_over_https_and_lets_it_sleep_on_exit(
        phone, instance, test_pattern_device, unique_label):
    """The wake lock needs a secure context, which only HTTPS gives a page reached by address
    (ADR 0014); before HTTPS was the default it had never been seen working."""
    label = unique_label("fs-wake")
    instance.add_device(test_pattern_device, label=label)
    phone.add_init_script(WATCH_WAKE_LOCK)
    # Chrome grants a visible page the wake lock by itself; headless Chromium, with no one to
    # grant it, denies it unless told. Only a secure context may ask at all.
    phone.context.grant_permissions(["screen-wake-lock"], origin=instance.base_url)
    tile = on_a_tile(phone, instance, label)
    assert phone.evaluate("window.isSecureContext && Boolean(navigator.wakeLock)")

    tile.get_by_role("button", name=f"Full Screen test-pattern · {label}").tap()

    expect(full_screen(phone)).to_be_visible()
    phone.wait_for_function("window.__wakeLocks.length > 0", timeout=5_000)
    assert phone.evaluate("window.__wakeLocks.map(({ type, held, error }) => ({ type, held, error }))") == [
        {"type": "screen", "held": True, "error": None}]

    full_screen(phone).get_by_role("button", name="Exit Full Screen").tap()
    phone.wait_for_function("window.__wakeLocks[0].sentinel.released", timeout=5_000)
