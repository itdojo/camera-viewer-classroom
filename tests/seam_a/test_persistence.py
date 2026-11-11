"""Seam A: Devices and Cameras survive an instance restart, in a config store
only the instance can read."""

import pytest

from harness.whep import plays, watching


@pytest.fixture(scope="module")
def persistent(instances):
    """An instance of this module's own, because these tests restart it."""
    return instances.start("persist")


def listed(instance):
    """What the instance stores, without live readings: a Device's clock skew is re-read
    after a restart, and a whole-second ``Date`` header makes it jitter by a second."""
    with instance.client() as api:
        devices = sorted(({k: v for k, v in d.items() if k != "clock_skew_seconds"}
                          for d in api.get("/api/devices").json()), key=lambda d: d["label"])
        cameras = sorted(api.get("/api/cameras").json(), key=lambda c: c["id"])
    return devices, cameras


def config_files(instance):
    """``<octal mode> <path>`` for every file in the instance's config folder."""
    result = instance.exec("find", "/config", "-type", "f", "-printf", "%m %p\\n")
    assert result.returncode == 0, result.stderr
    return result.stdout.split("\n")[:-1]


def test_devices_and_cameras_including_manual_ones_are_still_listed_after_a_restart_and_play(
        persistent, test_pattern_device, device_harness, unique_label):
    other = device_harness.add_device(unique_label("persist-dev"))
    first, second = unique_label("keep"), unique_label("keep")
    persistent.add_device(test_pattern_device, label=first)
    persistent.add_device(other, label=second)
    manual = unique_label("keep-manual")
    with persistent.client() as api:
        response = api.post("/api/cameras", json={"device": manual, "url": test_pattern_device.rtsps_url(),
                                                  "fingerprint": test_pattern_device.fingerprint})
        assert response.status_code == 201, response.text
    before = listed(persistent)

    persistent.restart()

    assert listed(persistent) == before
    with persistent.client() as api:
        for label in (first, second, manual):
            with watching(api, f"{label}/test-pattern") as feed:
                assert plays(feed), feed.text


def test_a_removed_device_stays_removed_after_a_restart(persistent, test_pattern_device, unique_label):
    label = unique_label("gone")
    persistent.add_device(test_pattern_device, label=label)
    with persistent.client() as api:
        assert api.delete(f"/api/devices/{label}").status_code == 204

    persistent.restart()

    devices, cameras = listed(persistent)
    assert label not in [d["label"] for d in devices]
    assert all(c["device"] != label for c in cameras)


def test_the_config_store_is_readable_only_by_the_instance(persistent, test_pattern_device, unique_label):
    persistent.add_device(test_pattern_device, label=unique_label("mode"))

    files = config_files(persistent)

    assert files, "the Devices were not written to the config folder"
    assert all(line.startswith("600 ") for line in files), files
