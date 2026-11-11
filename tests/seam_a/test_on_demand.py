"""Seam A: on-demand pulling. The Relay holds a connection to a Device only while some browser
has one of its Feeds on screen or a Recording holds it; otherwise the Feed is Idle and costs the
Device's link nothing.

Whether the Relay is connected is read from the Device's own API: its list of RTSPS connections
from other hosts, where the Relay pulling a Camera is the only one. Each test uses a Device of its
own, so no other test's viewers can be counted.
"""

import ipaddress
import time

from harness.whep import plays, watching


def rtsps_connections(device):
    """Every RTSPS connection the Device holds open from another host, whatever path it is for.
    Its own test-pattern publisher connects over loopback and is left out."""
    with device.api_client() as api:
        response = api.get("/v3/rtspsconns/list")
    response.raise_for_status()
    return [conn for conn in response.json()["items"]
            if not ipaddress.ip_address(conn["remoteAddr"].rsplit(":", 1)[0].strip("[]")).is_loopback]


def feed_status(instance, camera_id):
    with instance.client() as api:
        return next(c for c in api.get("/api/cameras").json() if c["id"] == camera_id)["feed_status"]


def test_a_feed_nobody_watches_or_records_is_idle_and_holds_no_connection_to_its_device(
        instance, own_device):
    device = own_device("unwatched")
    camera = f"{device.label}/test-pattern"

    deadline = time.monotonic() + 6  # several Feed Monitor ticks and Device probes
    while time.monotonic() < deadline:
        assert rtsps_connections(device) == []
        assert feed_status(instance, camera) == "idle"
        time.sleep(1)


def until(condition, timeout, message):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, message
        time.sleep(0.5)


def status_is(camera_id, expected):
    return lambda cameras: any(c["id"] == camera_id and c.get("feed_status") == expected for c in cameras)


def test_opening_a_feed_in_any_browser_starts_one_pull_and_closing_it_ends_the_pull(
        page, browser, instance, own_device):
    device = own_device("opened")
    camera = f"{device.label}/test-pattern"
    other = browser.new_context().new_page()

    with instance.events() as events:
        for viewer in (page, other):
            viewer.goto(instance.base_url)
            viewer.get_by_role("list", name="Cameras").get_by_role("button", name=device.label).click()
        events.next("cameras", where=status_is(camera, "live"), timeout=30)
        assert len(rtsps_connections(device)) == 1  # two browsers, one connection to the Device

        for viewer in (page, other):
            viewer.goto("about:blank")  # pagehide ends the WHEP session (see tests/conftest.py)
        other.context.close()
        until(lambda: rtsps_connections(device) == [], 30, "the Relay kept pulling a Feed nobody watches")
        events.next("cameras", where=status_is(camera, "idle"), timeout=30)


def test_an_idle_feed_plays_within_two_seconds_of_being_opened(instance, own_device):
    device = own_device("quick")
    camera = f"{device.label}/test-pattern"
    assert rtsps_connections(device) == []

    with instance.events() as events, instance.client() as api:
        opened = time.monotonic()
        with watching(api, camera) as response:
            answered = time.monotonic() - opened
            assert plays(response), response.text  # 201 means the Relay is receiving the Feed
            events.next("cameras", where=status_is(camera, "live"), timeout=10)
    assert answered < 2, f"the Relay took {answered:.1f} s to start pulling"


def test_a_recording_keeps_its_feed_pulled_after_every_browser_closes_it(page, instance, own_device):
    device = own_device("held")
    camera = f"{device.label}/test-pattern"

    with instance.events() as events, instance.client() as api:
        page.goto(instance.base_url)
        page.get_by_role("list", name="Cameras").get_by_role("button", name=device.label).click()
        events.next("cameras", where=status_is(camera, "live"), timeout=30)
        response = api.post("/api/recordings", json={"camera": camera})
        assert response.status_code == 201, response.text
        recording = response.json()
        try:
            page.goto("about:blank")  # the last browser closes the Feed
            # Past the Relay's 10 s on-demand close delay and the Feed Monitor's 15 s demand linger.
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                assert len(rtsps_connections(device)) == 1, "the Recording's Feed stopped being pulled"
                assert feed_status(instance, camera) == "live"
                time.sleep(1)
        finally:
            stopped = api.post(f"/api/recordings/{recording['id']}/stop")
        assert stopped.status_code == 200, stopped.text

        until(lambda: rtsps_connections(device) == [], 30, "the Relay kept pulling after the Recording stopped")
        events.next("cameras", where=status_is(camera, "idle"), timeout=30)
