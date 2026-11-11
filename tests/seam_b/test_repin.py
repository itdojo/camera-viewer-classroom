"""Seam B: a rebuilt Device shows "certificate changed" with its new fingerprint, and one click re-pins it."""

import re

from playwright.sync_api import expect

PLAYING = """() => {
    const video = document.querySelector('[aria-label="Single View"] video');
    return video && video.videoWidth > 0 && video.currentTime > 1;
}"""


def test_a_rebuilt_device_is_re_pinned_from_the_ui_and_its_feed_plays_again(
        page, instance, device_harness, unique_label):
    device = device_harness.add_device(unique_label("rebuilt"))
    instance.add_device(device)
    page.goto(instance.base_url)
    camera = page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(device.label))
    expect(camera).to_be_visible()

    new = device.regenerate_cert()

    expect(camera).to_contain_text("certificate changed", timeout=30_000)
    expect(camera).to_contain_text("Down")
    alert = page.get_by_role("region", name=re.compile(f"{device.label}.*certificate changed"))
    expect(alert).to_contain_text(":".join(re.findall("..", new)))
    alert.get_by_role("button", name="Trust new certificate").click()

    expect(alert).to_be_hidden(timeout=10_000)
    expect(camera).not_to_contain_text("certificate changed")
    camera.click()
    page.wait_for_function(PLAYING, timeout=20_000)
    with instance.client() as api:  # keep the shared instance's Camera list short
        api.delete(f"/api/devices/{device.label}")
    device_harness.remove_device(device)
