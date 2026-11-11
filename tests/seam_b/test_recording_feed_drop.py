"""Seam B: a Recording whose Feed drops stays a Recording, and every browser
says it is waiting for the Feed until the Feed comes back."""

import contextlib
import re

import pytest
from playwright.sync_api import expect


@pytest.fixture
def own_device(device_harness, unique_label):
    """A Device this test pauses; never the shared one."""
    device = device_harness.add_device(unique_label("drop-b"))
    yield device
    with contextlib.suppress(Exception):
        device.unpause()  # a test that failed mid-drop left it paused
    device_harness.remove_device(device)


def badge(page, label):
    item = page.get_by_role("list", name="Cameras").locator("li").filter(has_text=label)
    return item.locator(".badge-recording")


def test_every_browser_shows_waiting_for_feed_while_a_recorded_feed_is_down(
        browser, instance, own_device, unique_label):
    label = unique_label("drop-b")
    instance.add_device(own_device, label=label)
    pages = [browser.new_context().new_page() for _ in range(2)]
    for page in pages:
        page.goto(instance.base_url)

    with instance.client() as api:
        response = api.post("/api/recordings", json={"camera": f"{label}/test-pattern"})
        assert response.status_code == 201, response.text
        recording = response.json()
        for page in pages:
            expect(badge(page, label)).to_have_text("Recording", timeout=20_000)

        own_device.pause()
        try:
            for page in pages:
                expect(badge(page, label)).to_have_text(re.compile("waiting for Feed", re.I), timeout=40_000)
        finally:
            own_device.unpause()
        for page in pages:
            expect(badge(page, label)).to_have_text("Recording", timeout=60_000)

        assert api.post(f"/api/recordings/{recording['id']}/stop").status_code == 200
    for page in pages:
        expect(badge(page, label)).to_have_count(0)
        page.context.close()
