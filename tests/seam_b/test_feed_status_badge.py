"""Seam B: every browser shows each Feed's Feed Status, live, in ITdojo colors."""

import re

import pytest
from playwright.sync_api import expect


def camera_entry(page, label):
    return page.get_by_role("list", name="Cameras").locator("li", has_text=label)


def test_feed_status_updates_in_every_browser_without_reloading(
        browser, instance, test_pattern_device, unique_label):
    label = unique_label("badge")
    instance.add_device(test_pattern_device, label=label)
    watcher = browser.new_context().new_page()
    watcher.goto(instance.base_url)
    viewer = browser.new_context().new_page()
    viewer.goto(instance.base_url)

    for page in (watcher, viewer):
        expect(camera_entry(page, label).get_by_text("Idle", exact=True)).to_be_visible()

    camera_entry(viewer, label).get_by_role("button", name=re.compile(label)).click()  # not its grip, "Move"

    for page in (watcher, viewer):
        expect(camera_entry(page, label).get_by_text("Live", exact=True)).to_be_visible(timeout=30_000)
    header = viewer.get_by_role("region", name="Single View").locator("header")
    expect(header.get_by_text("Live", exact=True)).to_be_visible()
    for page in (watcher, viewer):
        page.goto("about:blank")  # pagehide ends the WHEP session (see tests/conftest.py)
        page.context.close()


def test_a_multi_view_tile_shows_its_feed_status(page, instance, test_pattern_device, unique_label):
    label = unique_label("tile")
    instance.add_device(test_pattern_device, label=label)
    page.goto(instance.base_url)

    page.get_by_role("list", name="Cameras").get_by_role(
        "checkbox", name=re.compile(rf"\b{label}\b")).check()

    tile = page.get_by_role("region", name="Multi View").get_by_role("article", name=re.compile(label))
    expect(tile.get_by_text("Live", exact=True)).to_be_visible(timeout=30_000)


BADGE_COLORS ="""async (status) => {
    const { feedStatusBadge } = await import('/js/feed-status.js');
    const badge = feedStatusBadge(status);
    document.body.append(badge);
    const dot = getComputedStyle(badge, '::before');
    const colors = {
        text: badge.textContent,
        color: getComputedStyle(badge).color,
        background: getComputedStyle(badge).backgroundColor,
        dot: dot.backgroundColor,
        ring: dot.borderTopColor,
    };
    badge.remove();
    return colors;
}"""


@pytest.mark.parametrize(("status", "text", "dot"), [
    ("live", "Live", "rgb(60, 184, 88)"),        # --jade
    ("degraded", "Degraded", "rgb(216, 156, 44)"),  # --warning (amber)
    ("down", "Down", "rgb(168, 51, 74)"),        # --danger
    ("idle", "Idle", "rgb(107, 101, 130)"),      # --gray-500, neutral
])
def test_each_feed_status_wears_its_design_system_color(page, instance, status, text, dot):
    page.emulate_media(color_scheme="light")
    page.goto(instance.base_url)

    badge = page.evaluate(BADGE_COLORS, status)

    assert badge["text"] == text
    assert badge["dot"] == dot


def test_offline_is_neutral_like_idle_but_visibly_different(page, instance):
    page.emulate_media(color_scheme="light")
    page.goto(instance.base_url)

    idle, offline = page.evaluate(BADGE_COLORS, "idle"), page.evaluate(BADGE_COLORS, "offline")

    assert offline["text"] == "Offline"
    assert offline["background"] == idle["background"]  # both neutral
    assert offline["ring"] == "rgb(107, 101, 130)"       # a hollow --gray-500 ring, not a filled dot
    assert offline["dot"] != idle["dot"]


def test_feed_status_badges_follow_the_dark_theme(page, instance):
    page.emulate_media(color_scheme="dark")
    page.goto(instance.base_url)

    assert page.evaluate(BADGE_COLORS, "live")["dot"] == "rgb(78, 206, 106)"     # dark --jade
    assert page.evaluate(BADGE_COLORS, "down")["dot"] == "rgb(224, 107, 130)"    # dark --danger
    assert re.fullmatch(r"rgb\(\d+, \d+, \d+\)", page.evaluate(BADGE_COLORS, "live")["color"])
