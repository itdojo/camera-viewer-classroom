"""Seam A: removing a Device by hand, when it is decommissioned."""

import time

from harness.whep import feed_url, plays, watching


def relay_readers(device, camera):
    """Who is reading a Camera on the Device itself: the Relay, while it pulls."""
    with device.api_client() as api:
        return api.get(f"/v3/paths/get/{camera}").json().get("readers") or []


def wait_until(predicate, timeout=10, interval=0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_removing_a_device_removes_its_cameras_everywhere_and_stops_the_relay_pulling(
        instance, device_harness, unique_label):
    device = device_harness.add_device(unique_label("rm-dev"))
    label = unique_label("rm")
    camera_id = f"{label}/test-pattern"
    instance.add_device(device, label=label)

    with instance.client() as api, watching(api, camera_id) as feed:
        assert plays(feed), feed.text
        assert wait_until(lambda: relay_readers(device, "test-pattern"), timeout=5)

        # One stream per resource: each sees every event, so their order doesn't matter.
        with instance.events() as camera_events, instance.events() as device_events:
            response = api.delete(f"/api/devices/{label}")
            assert response.status_code == 204
            camera_events.next("cameras", where=lambda cs: all(c["device"] != label for c in cs))
            device_events.next("devices", where=lambda ds: all(d["label"] != label for d in ds))

        assert wait_until(lambda: not relay_readers(device, "test-pattern"), timeout=5)
        assert all(d["label"] != label for d in api.get("/api/devices").json())
        assert all(c["device"] != label for c in api.get("/api/cameras").json())
        assert api.post(feed_url(camera_id), content=b"v=0").status_code == 404


def test_removing_a_device_that_does_not_exist_is_a_clear_404(instance):
    with instance.client() as api:
        response = api.delete("/api/devices/no-such-device")

        assert response.status_code == 404
        assert response.json()["error"] == "device_not_found"


def test_a_removed_label_can_be_used_again(instance, test_pattern_device, unique_label):
    label = unique_label("again")
    instance.add_device(test_pattern_device, label=label)

    with instance.client() as api:
        assert api.delete(f"/api/devices/{label}").status_code == 204
        instance.add_device(test_pattern_device, label=label)

        with watching(api, f"{label}/test-pattern") as feed:
            assert plays(feed), feed.text
