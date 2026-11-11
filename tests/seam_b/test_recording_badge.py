"""Seam B: a Recording belongs to the instance. Its badge shows in every
browser, on the Camera list and on the tile, and stays on its Camera when a
student switches away or closes the tab."""

import re
import time

from playwright.sync_api import expect


def camera_item(page, label):
    return page.get_by_role("list", name="Cameras").locator("li").filter(has_text=label)


def tile(page, label):
    return page.get_by_role("region", name="Multi View").get_by_role("article", name=re.compile(rf"\b{label}\b"))


def test_the_recording_badge_follows_the_camera_not_the_view_and_outlives_the_tab(
        browser, instance, test_pattern_device, unique_label):
    recorded, other = unique_label("rec"), unique_label("other")
    instance.add_device(test_pattern_device, label=recorded)
    instance.add_device(test_pattern_device, label=other)
    student = browser.new_context().new_page()
    student.goto(instance.base_url)
    teammate = browser.new_context().new_page()
    teammate.goto(instance.base_url)
    teammate.get_by_role("list", name="Cameras").get_by_role(
        "checkbox", name=re.compile(rf"\b{recorded}\b")).check()  # on screen in the teammate's Multi View
    expect(tile(teammate, recorded)).to_be_visible()

    cameras = student.get_by_role("list", name="Cameras")
    cameras.get_by_role("button", name=re.compile(recorded)).click()
    single = student.get_by_role("region", name="Single View")
    single.get_by_role("button", name="Record").click()
    expect(single.get_by_text("Recording", exact=True)).to_be_visible(timeout=10_000)

    cameras.get_by_role("button", name=re.compile(other)).click()  # switch Cameras
    expect(single.get_by_role("heading", name=re.compile(other))).to_be_visible()
    expect(single.get_by_text("Recording", exact=True)).to_be_hidden()
    for page in (student, teammate):
        expect(camera_item(page, recorded).get_by_text("Recording", exact=True)).to_be_visible()
        expect(camera_item(page, other).get_by_text("Recording", exact=True)).to_have_count(0)
    expect(tile(teammate, recorded).get_by_text("Recording", exact=True)).to_be_visible()

    student.context.close()  # the tab that started it goes away
    time.sleep(2)
    with instance.client() as api:
        [recording] = [r for r in api.get("/api/recordings").json() if r["camera"].startswith(recorded)]
    assert recording["state"] == "active"
    expect(camera_item(teammate, recorded).get_by_text("Recording", exact=True)).to_be_visible()

    tile(teammate, recorded).get_by_role("button", name="Stop recording").click()  # from the tile
    expect(tile(teammate, recorded).get_by_role("button", name="Record")).to_be_visible(timeout=20_000)
    expect(teammate.get_by_role("region", name="Multi View")).to_be_visible()  # the click stayed on the tile
    expect(camera_item(teammate, recorded).locator(".badge")).to_have_count(0)
    expect(tile(teammate, recorded).locator(".badge")).to_have_count(0)
    assert any(p.name.startswith(f"{recorded}-test-pattern-") and p.suffix == ".mp4"
               for p in instance.captures_dir.iterdir())
    teammate.context.close()
