"""Seam B: the Device setup guide (#100) opens in its own tab from where a
student adds a Device, shows the steps for their camera, and needs nothing
outside the instance."""

import re

from playwright.sync_api import expect

GUIDE = "Device setup guide"
STEPS = 10  # the main path; the variants change lines, never the step numbers a step refers to


def open_guide_from(page, region):
    with page.context.expect_page() as opened:
        region.get_by_role("link", name=GUIDE).click()
    guide = opened.value
    guide.wait_for_load_state()
    return guide


def test_the_add_device_panel_opens_the_guide_in_a_new_tab(page, instance):
    page.goto(instance.base_url)
    panel = page.get_by_role("region", name="Add Device")

    guide = open_guide_from(page, panel)

    expect(guide.get_by_role("heading", name="Set up a Device")).to_be_visible()
    expect(page.get_by_role("region", name="Add Device")).to_be_visible()  # the UI stays where it was


def test_the_management_bars_footer_opens_the_guide_whatever_the_panels_show(page, instance):
    # The Panels' links go when Cameras are listed or Add Device is collapsed;
    # the footer's stays.
    page.goto(instance.base_url)
    footer = page.get_by_role("complementary", name="Management Bar").locator("footer")

    guide = open_guide_from(page, footer)

    expect(guide.get_by_role("heading", name="Set up a Device")).to_be_visible()


def test_an_instance_with_no_cameras_points_to_the_guide(browser, instances, unique_label):
    empty = instances.start(unique_label("guide-empty"))
    page = browser.new_page()
    page.goto(empty.base_url)
    cameras = page.get_by_role("region", name="Cameras")
    expect(cameras.get_by_text("No Cameras yet")).to_be_visible()

    guide = open_guide_from(page, cameras)

    expect(guide.get_by_role("heading", name="Set up a Device")).to_be_visible()


def visible_shell_lines(page):
    # :visible, since innerText falls back to the text of a hidden element.
    return [line.strip() for line in page.locator(".guide-shell code:visible").all_inner_texts()]


def test_the_guide_starts_on_the_usb_webcam_path(page, instance):
    page.goto(f"{instance.base_url}/setup.html")

    expect(page.get_by_role("button", name="USB webcam")).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".guide-steps > li:visible")).to_have_count(STEPS)
    lines = visible_shell_lines(page)
    assert "cd device-kit/usb" in lines
    assert "ls /dev/v4l/by-id/" in lines
    assert "rpicam-hello --list-cameras" not in lines
    assert "docker compose --profile gps up -d" in lines  # most Devices have GPS, so it is on the main path


def test_choosing_the_camera_module_swaps_in_its_lines(page, instance):
    page.goto(f"{instance.base_url}/setup.html")

    page.get_by_role("button", name="Pi camera module").click()

    expect(page.get_by_role("button", name="Pi camera module")).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_role("button", name="USB webcam")).to_have_attribute("aria-pressed", "false")
    expect(page.locator(".guide-steps > li:visible")).to_have_count(STEPS)
    lines = visible_shell_lines(page)
    assert "cd device-kit/csi" in lines
    assert "rpicam-hello --list-cameras" in lines
    assert "cd device-kit/usb" not in lines
    expect(page.get_by_text("Power the Pi off first.")).to_be_visible()
    expect(page.get_by_text(re.compile("CAMERA_HFLIP=true"))).to_be_attached()


def test_no_camera_is_the_test_pattern_dry_run(page, instance):
    page.goto(f"{instance.base_url}/setup.html")

    page.get_by_role("button", name="No camera").click()

    expect(page.locator(".guide-steps > li:visible")).to_have_count(STEPS)
    assert "cd device-kit/test-pattern" in visible_shell_lines(page)
    expect(page.get_by_text("There is no camera to attach")).to_be_visible()
    expect(page.get_by_text("Flip the picture")).to_be_hidden()


def test_the_guide_loads_offline(offline, instance):
    page = offline.new_page()
    page.goto(f"{instance.base_url}/setup.html")

    expect(page.get_by_role("heading", name="Set up a Device")).to_be_visible()
    expect(page.get_by_role("link", name="Download the Device Kit")).to_have_attribute("href", "device-kit.tar.gz")
    assert offline.blocked == []
