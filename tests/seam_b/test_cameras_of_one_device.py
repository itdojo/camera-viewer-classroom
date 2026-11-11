"""Seam B: every Camera on a Device shows that Device's Position and Trail (story 20, #55).

A Position belongs to the Device, never to one Camera. Kit Devices carry one Camera each, so the
second here is a manual Camera joined to the GPS Device by its own RTSPS URL, the cheapest real
way to put two Cameras on one Device. The Device replays a one-epoch track, so its Sidecar sends
the same fix every second and the coordinates are a known literal. The browser can reach the
instance and nothing else (``harness.offline``).
"""

import re
import time

import pytest
from playwright.sync_api import expect

from harness.gps import Epoch, track_lines, write_track

TRACK = "still.nmea"
EPOCH = Epoch("093005.00", 51.50001, -0.12346, 35.4, 5.0, 47.0)
COORDINATES = "51.50001, -0.12346"
SECOND = "second"


@pytest.fixture
def gps_device(instance, device_harness, unique_label, tmp_path):
    """A GPS Device with its kit Camera and a second, manual Camera from the same host."""
    tracks = write_track(tmp_path / "tracks", track_lines([EPOCH]), TRACK)
    device = device_harness.add_device(unique_label("pair"), gps=True, gps_tracks=tracks,
                                       settings={"GPS_SOURCE": f"replay:{TRACK}"})
    instance.add_device(device)
    with instance.client() as api:
        joined = api.post("/api/cameras", json={"device": device.label, "name": SECOND,
                                                "url": device.rtsps_url(credentials=False)})
        assert joined.status_code == 201, joined.text
        # Multi View alone streams at the idle rate; renew Position Demand so a fix comes each second.
        assert api.put(f"/api/devices/{device.label}/position-demand").status_code == 204
    yield device
    with instance.client() as api:
        api.delete(f"/api/devices/{device.label}")


def camera_name(camera, label):
    """A tile's or a checkbox's name for the Camera: "<camera> · <device>"."""
    return re.compile(rf"\b{camera} · {label}\b")


def list_button(cameras, camera, label):
    """The Camera's button in the Cameras list, named by its name, Device and Feed Status."""
    return cameras.get_by_role("button", name=re.compile(rf"^{camera}\s*{label}\b"))


def test_both_tiles_show_the_device_position(offline, instance, gps_device):
    label = gps_device.label
    page = offline.new_page()
    page.goto(instance.base_url)
    cameras = page.get_by_role("list", name="Cameras")
    multi_view = page.get_by_role("region", name="Multi View")

    for camera in ("test-pattern", SECOND):
        cameras.get_by_role("checkbox", name=camera_name(camera, label)).check()

    for camera in ("test-pattern", SECOND):
        position = multi_view.get_by_role("article", name=camera_name(camera, label)).get_by_role(
            "group", name="Position")
        expect(position.get_by_text(COORDINATES, exact=True)).to_be_visible(timeout=15_000)
        expect(position.locator("[data-fix]")).to_have_attribute("data-fix", "live")
    assert offline.blocked == []


def test_switching_between_cameras_of_one_device_keeps_its_position_and_trail(offline, instance, gps_device):
    label = gps_device.label
    # The still track repeats one fix, which extends no line live, so the Trail the map draws is the
    # instance's: wait until it holds a few fixes.
    deadline = time.monotonic() + 20
    with instance.client() as api:
        while len(api.get(f"/api/devices/{label}/trail").json()["fixes"]) < 3:
            assert time.monotonic() < deadline
            time.sleep(0.5)
    page = offline.new_page()
    page.goto(instance.base_url)
    cameras = page.get_by_role("list", name="Cameras")
    single_view = page.get_by_role("region", name="Single View")
    position = single_view.locator("header").get_by_role("group", name="Position")
    marker = single_view.get_by_role("region", name="Map", exact=True).get_by_role(
        "img", name=re.compile(rf"^{label}: {COORDINATES}, "))

    list_button(cameras, "test-pattern", label).click()
    expect(position.get_by_text(COORDINATES, exact=True)).to_be_visible(timeout=15_000)
    expect(marker).to_be_visible(timeout=15_000)
    deadline = time.monotonic() + 15
    while (drawn := int(marker.get_attribute("data-trail") or 0)) < 3:
        assert time.monotonic() < deadline, f"the Trail holds {drawn} fixes"
        page.wait_for_timeout(200)

    list_button(cameras, SECOND, label).click()

    expect(single_view.locator("header")).to_contain_text(SECOND)
    expect(position.get_by_text(COORDINATES, exact=True)).to_be_visible()
    expect(marker).to_be_visible()
    assert int(marker.get_attribute("data-trail") or 0) >= drawn  # the same Trail, not a new one
    assert offline.blocked == []
