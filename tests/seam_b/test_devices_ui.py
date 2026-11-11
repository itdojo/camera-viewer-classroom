"""Seam B: the Devices Panel and the Add Device form: removing after confirming, the
instance's refusals, and a Device clock too far off to pull."""

import re

from playwright.sync_api import expect


def test_removing_a_device_asks_first_then_its_cameras_leave_every_browser(
        browser, instance, test_pattern_device, unique_label):
    label = unique_label("ui-rm")
    instance.add_device(test_pattern_device, label=label)
    watcher = browser.new_context().new_page()
    watcher.goto(instance.base_url)
    student = browser.new_context().new_page()
    student.goto(instance.base_url)
    remove = student.get_by_role("list", name="Devices").get_by_role("button", name=f"Remove {label}")

    student.once("dialog", lambda dialog: dialog.dismiss())
    remove.click()
    expect(watcher.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(label))).to_be_visible()

    student.once("dialog", lambda dialog: dialog.accept())
    remove.click()
    for page in (student, watcher):
        expect(page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(label))).to_have_count(0)
        expect(page.get_by_role("list", name="Devices").get_by_text(label)).to_have_count(0)
    watcher.context.close()
    student.context.close()


def test_a_label_the_instance_refuses_shows_its_message(page, instance, test_pattern_device):
    page.goto(instance.base_url)
    form = page.get_by_role("form", name="Add Device")
    # The browser's own pattern check would catch this one first; skip it to
    # show the instance's answer is displayed.
    form.evaluate("form => form.noValidate = true")
    form.get_by_label("Address").fill(test_pattern_device.address)
    form.get_by_label("Label", exact=True).fill("Not_Kebab")
    form.get_by_label("Username").fill(test_pattern_device.username)
    form.get_by_label("Password").fill(test_pattern_device.password)
    form.get_by_role("button", name="Check fingerprint").click()

    expect(form.get_by_role("alert")).to_contain_text("kebab-case")


def test_adding_a_device_with_a_wrong_clock_warns_and_its_entry_says_how_to_fix_it(
        page, instance, instance_image, device_harness, unique_label):
    label = unique_label("ui-clock")
    device = device_harness.add_scripted_device(label, instance_image, clock_offset=-(3 * 3600 + 35 * 60), paths=[
        {"name": "test-pattern", "available": True, "tracks2": []}])
    try:
        page.goto(instance.base_url)
        form = page.get_by_role("form", name="Add Device")
        form.get_by_label("Address").fill(device.address)
        form.get_by_label("Label", exact=True).fill(label)
        form.get_by_label("Username").fill(device.username)
        form.get_by_label("Password").fill(device.password)
        form.get_by_role("button", name="Check fingerprint").click()
        form.get_by_role("button", name="Confirm fingerprint and add Device").click()

        warning = re.compile(rf"{label}'s clock is 3 h 3[45] min behind this instance's.*Enable NTP")
        expect(form.get_by_role("status")).to_have_text(warning)
        entry = page.get_by_role("list", name="Devices").get_by_role("listitem").filter(has_text=label)
        expect(entry).to_contain_text(warning)
    finally:
        with instance.client() as api:
            api.delete(f"/api/devices/{label}")
        device_harness.remove_scripted_device(device)
