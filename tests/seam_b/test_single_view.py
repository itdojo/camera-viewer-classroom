"""Seam B: a Feed plays in a real browser, through the Relay and the WHEP proxy."""

import re

from playwright.sync_api import expect

PLAYING = """() => {
    const video = document.querySelector('[aria-label="Single View"] video');
    return video && video.videoWidth > 0 && video.currentTime > 1;
}"""


def open_camera(page, instance, label):
    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(label)).click()


def test_clicking_a_camera_plays_its_feed_at_the_device_resolution(
        page, instance, test_pattern_device, unique_label):
    label = unique_label("play")
    instance.add_device(test_pattern_device, label=label)

    open_camera(page, instance, label)
    page.wait_for_function(PLAYING, timeout=20_000)

    size = page.evaluate("""() => {
        const video = document.querySelector('[aria-label="Single View"] video');
        return [video.videoWidth, video.videoHeight];
    }""")
    assert size == [1280, 720]
    frames = page.evaluate("""async () => {
        const video = document.querySelector('[aria-label="Single View"] video');
        const before = video.getVideoPlaybackQuality().totalVideoFrames;
        await new Promise(resolve => setTimeout(resolve, 1000));
        return video.getVideoPlaybackQuality().totalVideoFrames - before;
    }""")
    assert frames >= 15  # the test pattern runs at 30 fps
    expect(page.get_by_role("heading", name=re.compile(label))).to_be_visible()


def test_a_device_added_in_one_browser_appears_in_every_other_without_reloading(
        browser, instance, test_pattern_device, unique_label):
    label = unique_label("ui")
    watcher = browser.new_context().new_page()
    watcher.goto(instance.base_url)
    student = browser.new_context().new_page()
    student.goto(instance.base_url)

    form = student.get_by_role("form", name="Add Device")
    form.get_by_label("Address").fill(test_pattern_device.address)
    form.get_by_label("Label", exact=True).fill(label)
    form.get_by_label("Username").fill(test_pattern_device.username)
    form.get_by_label("Password").fill(test_pattern_device.password)
    form.get_by_role("button", name="Check fingerprint").click()
    fingerprint = test_pattern_device.fingerprint
    expect(form.get_by_text(":".join(re.findall("..", fingerprint)))).to_be_visible()
    form.get_by_role("button", name="Confirm fingerprint and add Device").click()

    for page in (student, watcher):
        cameras = page.get_by_role("list", name="Cameras")
        expect(cameras.get_by_role("button", name=re.compile(label))).to_be_visible()
    watcher.context.close()
    student.context.close()


def test_the_relay_pulls_the_feed_over_rtsps_tcp_as_the_instance_user(
        page, instance, device_harness, unique_label):
    device = device_harness.add_device(unique_label("tcp"))  # its own, so its readers are ours alone
    instance.add_device(device)

    open_camera(page, instance, device.label)
    page.wait_for_function(PLAYING, timeout=20_000)

    with device.api_client() as api:
        sessions = api.get("/v3/rtspssessions/list").json()["items"]  # RTSPS: the kit serves nothing else
    readers = [s for s in sessions if s["state"] == "read"]
    assert [(r["path"], r["transport"], r["user"]) for r in readers] == [("test-pattern", "TCP", device.username)]


def test_the_relay_refuses_a_device_whose_certificate_no_longer_matches_the_pin(
        page, instance, device_harness, unique_label):
    # With the first test, this shows the pin is real: a self-signed
    # certificate plays when it matches the pin and is refused when it doesn't.
    device = device_harness.add_device(unique_label("rebuilt"))
    instance.add_device(device)
    device.regenerate_cert()  # rebuilt with a new certificate after it was pinned

    open_camera(page, instance, device.label)

    expect(page.get_by_text(re.compile("Feed unavailable"))).to_be_visible(timeout=30_000)
    with device.api_client() as api:
        sessions = api.get("/v3/rtspssessions/list").json()["items"]
    assert [s for s in sessions if s["state"] == "read"] == []


KILL_RELAY = """
import os, signal
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        if open(f"/proc/{pid}/comm").read().strip() == "mediamtx":
            os.kill(int(pid), signal.SIGKILL)
    except OSError:
        pass
"""


def test_a_relay_restart_loses_no_camera(page, instances, test_pattern_device, unique_label):
    own = instances.start(unique_label("relay-restart"))
    label = unique_label("survivor")
    own.add_device(test_pattern_device, label=label)
    open_camera(page, own, label)
    page.wait_for_function(PLAYING, timeout=20_000)

    assert own.exec("python", "-c", KILL_RELAY).returncode == 0  # the Relay crashes; the supervisor restarts it

    page.reload()
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(label)).click()
    page.wait_for_function(PLAYING, timeout=30_000)
