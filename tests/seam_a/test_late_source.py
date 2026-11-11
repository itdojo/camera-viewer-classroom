"""Seam A: a Device added while its camera is not working yet (#36).

A Camera is every path the Device configures, live or not. So a CSI Device
whose ribbon is badly seated is added with its Camera, which shows Down, and
plays once the camera works, without being removed and added again. The
Device is the CSI stand-in whose source waits for a file before publishing
(``tests/harness/csi-stand-in-held.yaml``).
"""

import re

import pytest


def status_is(camera_id, expected):
    return lambda cameras: any(c["id"] == camera_id and c.get("feed_status") == expected for c in cameras)


@pytest.fixture
def held_device(instance, device_harness, unique_label):
    """A CSI stand-in that is up but not yet publishing, added to the shared
    instance; removed from both afterwards."""
    device = device_harness.add_device(unique_label("held"), variant="csi-stand-in-held", wait=False)
    try:
        device.wait_ready(publishing=False)
        with device.api_client() as api:
            assert api.get("/v3/paths/get/csi").json()["available"] is False
        created = instance.add_device(device)
        yield device, created
    finally:
        with instance.client() as api:
            api.delete(f"/api/devices/{device.label}")
        device_harness.remove_device(device)


def test_a_device_added_while_its_source_is_down_lists_its_camera(instance, held_device):
    device, created = held_device

    assert created["cameras"] == [f"{device.label}/csi"]
    with instance.client() as api:
        listed = [c["id"] for c in api.get("/api/cameras").json() if c["device"] == device.label]
    assert listed == [f"{device.label}/csi"]


def test_its_watched_feed_is_down_until_the_source_comes_up_then_live(page, instance, held_device):
    device, _ = held_device
    camera = f"{device.label}/csi"

    with instance.events() as events:
        page.goto(instance.base_url)
        page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(device.label)).click()
        events.next("cameras", where=status_is(camera, "down"), timeout=30)
        device.exec("touch", "/tmp/camera-up")
        events.next("cameras", where=status_is(camera, "live"), timeout=60)
