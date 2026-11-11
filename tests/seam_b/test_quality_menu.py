"""Seam B: the Quality gear menu on every Feed (spec #46, #47).

Cameras here are on the scripted Device API, a CSI Device with no media, so the menu, its
keyboard behaviour and what every browser shows can be driven without a camera (the Offline test
uses a test-pattern Device, which can be stopped). The shared
instance checks Devices every 10 s, so anything a check reveals gets a 30 s timeout.
"""

import re

import pytest
from playwright.sync_api import expect

CSI_MEDIUM = {
    "name": "csi", "source": "rpiCamera", "rpiCameraWidth": 1920, "rpiCameraHeight": 1080,
    "rpiCameraFPS": 15, "rpiCameraCodec": "auto", "rpiCameraBitrate": 1500000, "rpiCameraIDRPeriod": 15,
}
CHECK = 30_000
GEAR = re.compile(r"^Quality\b")  # its name carries the level: "Quality: Medium · auto"


@pytest.fixture
def scripted_csi(instance, instance_image, device_harness, unique_label):
    label = unique_label("gear")
    device = device_harness.add_scripted_device(
        label, instance_image, paths=[{"name": "csi", "available": True}], path_configs={"csi": CSI_MEDIUM})
    instance.add_device(device, label=label)
    yield device, label
    with instance.client() as api:
        api.delete(f"/api/devices/{label}")


def single_view(page, instance, label):
    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(rf"\b{label}\b")).click()
    return page.get_by_role("region", name="Single View").locator("header")


def shows(where, text):
    """The Quality label as a viewer reads it on the Feed (the menu's items are hidden text)."""
    return where.get_by_text(text, exact=True).filter(visible=True)


def test_the_gear_menu_on_a_tile_works_by_keyboard_and_every_browser_sees_the_level(
        browser, instance, scripted_csi):
    _, label = scripted_csi
    viewer = browser.new_context().new_page()
    viewer.goto(instance.base_url)
    viewer.get_by_role("list", name="Cameras").get_by_role(
        "checkbox", name=re.compile(rf"\b{label}\b")).check()
    # Close the (harness-seeded) Management Bar: open, it lies over the tile's controls row.
    viewer.get_by_role("button", name="Management Bar").click()
    tile = viewer.get_by_role("region", name="Multi View").get_by_role("article", name=re.compile(label))
    watcher = browser.new_context().new_page()
    header = single_view(watcher, instance, label)
    expect(shows(tile, "Medium · auto")).to_be_visible(timeout=CHECK)  # read from the Device

    gear = tile.get_by_role("button", name=GEAR)
    expect(gear).to_have_accessible_name("Quality: Medium · auto")
    gear.focus()
    viewer.keyboard.press("Enter")
    menu = tile.get_by_role("menu", name="Quality")
    expect(menu).to_be_visible()
    auto = menu.get_by_role("menuitemradio", name="Auto")
    expect(auto).to_be_focused()  # the checked item
    expect(auto).to_have_attribute("aria-checked", "true")
    viewer.keyboard.press("Escape")
    expect(menu).to_be_hidden()
    expect(gear).to_be_focused()
    expect(viewer.get_by_role("region", name="Multi View")).to_be_visible()  # Esc closed only the menu

    viewer.keyboard.press("ArrowDown")
    for _ in range(3):
        viewer.keyboard.press("ArrowDown")  # High, Medium, Low
    expect(menu.get_by_role("menuitemradio", name="Low")).to_be_focused()
    viewer.keyboard.press("Enter")

    expect(menu).to_be_hidden()
    expect(shows(tile, "Low")).to_be_visible()
    expect(gear).to_have_accessible_name("Quality: Low")
    expect(shows(header, "Low")).to_be_visible()  # the other browser, no reload

    gear.click()
    menu.get_by_role("menuitemradio", name="Auto").click()

    expect(shows(tile, "Low · auto")).to_be_visible()
    expect(shows(header, "Low · auto")).to_be_visible()
    gear.click()
    expect(menu.get_by_role("menuitemradio", name="Auto")).to_have_attribute("aria-checked", "true")
    expect(menu.get_by_role("menuitemradio", name="Low")).to_have_attribute("aria-checked", "false")
    for page in (viewer, watcher):
        page.goto("about:blank")
        page.context.close()


def test_the_menu_is_disabled_with_a_reason_on_an_rtsp_url_camera(page, instance, unique_label):
    label = unique_label("by-url")
    with instance.client() as api:
        added = api.post("/api/cameras", json={"device": label, "url": "rtsp://10.0.0.9:8554/cam"})
        assert added.status_code == 201, added.text
    try:
        header = single_view(page, instance, label)

        gear = header.get_by_role("button", name=GEAR)
        expect(gear).to_be_disabled()
        expect(gear).to_have_accessible_description(re.compile("RTSP URL"))
    finally:
        with instance.client() as api:
            api.delete(f"/api/devices/{label}")


def test_the_menu_is_disabled_while_the_device_is_offline(page, instance, device_harness, unique_label):
    """Story 34, on a real kit Device, since a scripted one has no container to stop."""
    device = device_harness.add_device(unique_label("gear-off"))
    instance.add_device(device)
    try:
        header = single_view(page, instance, device.label)
        gear = header.get_by_role("button", name=GEAR)
        expect(gear).to_be_enabled()

        device.stop()

        expect(gear).to_be_disabled(timeout=CHECK)
        expect(gear).to_have_accessible_description(re.compile("Offline"))
        device.start()
        expect(gear).to_be_enabled(timeout=CHECK)
    finally:
        page.goto("about:blank")
        with instance.client() as api:
            api.delete(f"/api/devices/{device.label}")
        device_harness.remove_device(device)


def test_a_change_from_another_instance_shows_on_the_feed_until_a_level_is_picked(
        page, instances, instance_image, device_harness, unique_label):
    """A Device that keeps running has its configs read only before a write, so the change is
    made while this instance is down and found by its first check after the restart."""
    own = instances.start(unique_label("gear-other"))
    label = unique_label("gear")
    device = device_harness.add_scripted_device(
        label, instance_image, paths=[{"name": "csi", "available": True}], path_configs={"csi": CSI_MEDIUM})
    own.add_device(device, label=label)
    with own.client() as api:
        assert api.put(f"/api/cameras/{label}/csi/quality", json={"mode": "low"}).status_code == 200
    with device.api_client() as other:
        other.patch("/v3/config/paths/patch/csi", json={"rpiCameraFPS": 30}).raise_for_status()

    try:
        own.restart()
        header = single_view(page, own, label)

        notice = header.get_by_text("Changed by another instance")
        expect(notice).to_be_visible(timeout=CHECK)
        header.get_by_role("button", name=GEAR).click()
        header.get_by_role("menuitemradio", name="Medium").click()
        expect(notice).to_be_hidden()
        expect(shows(header, "Medium")).to_be_visible()
    finally:
        page.goto("about:blank")
        instances.remove(own)


def test_a_change_that_splits_the_recording_asks_first(page, instance, scripted_csi):
    device, label = scripted_csi
    camera = f"{label}/csi"
    header = single_view(page, instance, label)
    expect(shows(header, "Medium · auto")).to_be_visible(timeout=CHECK)
    with instance.client() as api:
        recording = api.post("/api/recordings", json={"camera": camera})
        assert recording.status_code == 201, recording.text
    try:
        gear = header.get_by_role("button", name=GEAR)
        gear.click()
        header.get_by_role("menuitemradio", name="Low").click()  # 1080p to 720p drops viewers

        confirm = header.get_by_role("group", name=re.compile("This splits the Recording"))
        expect(confirm).to_be_visible()
        expect(confirm.get_by_role("button", name="Change and split")).to_be_focused()
        confirm.get_by_role("button", name="Cancel").click()
        expect(confirm).to_be_hidden()
        expect(gear).to_be_focused()
        assert device.patches() == []

        gear.click()
        header.get_by_role("menuitemradio", name="Low").click()
        header.get_by_role("button", name="Change and split").click()

        expect(shows(header, "Low")).to_be_visible()
        assert [p["body"]["rpiCameraWidth"] for p in device.patches()] == [1280]
    finally:
        with instance.client() as api:
            api.post(f"/api/recordings/{recording.json()['id']}/stop")
