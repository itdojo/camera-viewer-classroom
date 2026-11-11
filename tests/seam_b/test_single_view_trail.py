"""Seam B: the Device's Trail on the Single View map, offline (#60).

The map draws a marker's Trail as a line layer on its canvas, out of reach of the DOM, so each
marker element carries ``data-trail``: how many fixes its Trail line holds once the line layer
has them (0 until it has two). The Device replays a track heading due north, one epoch a second,
so its Trail grows by about one fix a second. The browser reaches the instance and nothing else.
"""

import re
import time

import pytest
from playwright.sync_api import expect

from harness.gps import Epoch, track_lines, write_track

TRACK = "trail-north.nmea"
START = 51.50000
STEP = 20 / 111_320  # degrees of latitude in one second at 20 m/s
EPOCHS = [Epoch(f"1000{second:02d}.00", START + second * STEP, -0.12346, 35.0, 20.0, 0.0)
          for second in range(60)]
HISTORY = 12  # fixes the instance holds before the page opens
FIXED = {"latitude": 51.60000, "longitude": -0.30000}
TRAIL = "**/api/devices/*/trail"


@pytest.fixture
def gps_device(instance, device_harness, unique_label, tmp_path):
    tracks = write_track(tmp_path / "tracks", track_lines(EPOCHS), TRACK)
    device = device_harness.add_device(unique_label("trail"), gps=True, gps_tracks=tracks,
                                       settings={"GPS_SOURCE": f"replay:{TRACK}"})
    instance.add_device(device)
    yield device
    with instance.client() as api:
        api.delete(f"/api/devices/{device.label}")


def wait_for_history(instance, label, count=HISTORY, timeout=40):
    deadline = time.monotonic() + timeout
    with instance.client() as api:
        # An unwatched Device streams a fix every 30 s, so stand in for a viewer elsewhere and
        # renew its Position Demand (#64): HISTORY fixes then take HISTORY seconds, within the linger.
        assert api.put(f"/api/devices/{label}/position-demand").status_code == 204
        while len(api.get(f"/api/devices/{label}/trail").json()["fixes"]) < count:
            assert time.monotonic() < deadline
            time.sleep(0.5)


def open_single_view(page, instance, label):
    """Open the Device's Camera in Single View and return its map marker."""
    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(rf"\b{label}\b")).click()
    map_region = page.get_by_role("region", name="Single View").get_by_role("region", name="Map", exact=True)
    return map_region.get_by_role("img", name=re.compile(rf"^{label}: "))


def trail_length(marker):
    return int(marker.get_attribute("data-trail") or 0)


def wait_for_trail(marker, count, timeout):
    """Wait until the marker's Trail line holds ``count`` fixes or more; return how many."""
    deadline = time.monotonic() + timeout
    while (length := trail_length(marker)) < count:
        assert time.monotonic() < deadline, f"the Trail holds {length} fixes, not {count}"
        time.sleep(0.1)
    return length


def test_the_trail_is_drawn_from_history_as_soon_as_the_map_opens(offline, instance, gps_device):
    wait_for_history(instance, gps_device.label)
    page = offline.new_page()

    opened = time.monotonic()
    marker = open_single_view(page, instance, gps_device.label)

    # Live fixes alone would take HISTORY seconds to make a Trail this long.
    expect(marker).to_be_visible(timeout=15_000)
    wait_for_trail(marker, HISTORY, timeout=HISTORY - (time.monotonic() - opened) - 1)
    assert offline.blocked == []


def test_the_trail_grows_from_live_fixes_without_waiting_for_its_history(offline, instance, gps_device):
    wait_for_history(instance, gps_device.label)
    page = offline.new_page()
    held = []
    page.route(TRAIL, lambda route: held.append(route))  # the history request waits until released

    marker = open_single_view(page, instance, gps_device.label)

    expect(marker).to_be_visible(timeout=15_000)  # the marker is not kept waiting for the Trail
    assert wait_for_trail(marker, 3, timeout=10) < HISTORY  # live fixes only
    assert len(held) == 1

    held[0].fallback()

    grown = wait_for_trail(marker, HISTORY + 3, timeout=5)
    wait_for_trail(marker, grown + 1, timeout=5)  # and live fixes keep extending it
    assert offline.blocked == []


def test_a_fix_in_both_the_trail_so_far_and_the_live_fixes_is_drawn_once(offline, instance, gps_device):
    wait_for_history(instance, gps_device.label)
    page = offline.new_page()
    held = []
    page.route(TRAIL, lambda route: held.append(route))
    marker = open_single_view(page, instance, gps_device.label)
    expect(marker).to_be_visible(timeout=15_000)
    wait_for_trail(marker, 3, timeout=10)  # live fixes, which the instance's Trail holds as well

    # With the Sidecar stopped no fix arrives any more, so the Trail the held request is about to
    # answer is exactly the one read here, and it already holds every live fix the page drew.
    gps_device.stop_gps()
    page.wait_for_timeout(2_000)
    with instance.client() as api:
        fixes = len(api.get(f"/api/devices/{gps_device.label}/trail").json()["fixes"])
    held[0].fallback()

    expect(marker).to_have_attribute("data-trail", str(fixes), timeout=5_000)
    page.wait_for_timeout(1_000)
    expect(marker).to_have_attribute("data-trail", str(fixes))
    assert offline.blocked == []


def test_a_fixed_position_shown_over_a_stale_fix_draws_no_trail(offline, instance, gps_device):
    wait_for_history(instance, gps_device.label, count=3)
    page = offline.new_page()
    marker = open_single_view(page, instance, gps_device.label)
    expect(marker).to_be_visible(timeout=15_000)
    wait_for_trail(marker, 3, timeout=10)

    # Single View renews the Device's Position Demand, so its last fix goes stale 10 s after the
    # Sidecar stops, and the Fixed Position shows while the instance still holds the Trail.
    with instance.client() as api:
        assert api.put(f"/api/devices/{gps_device.label}/fixed-position", json=FIXED).status_code == 200
    gps_device.stop_gps()

    expect(marker).to_have_accessible_name(re.compile(r": 51\.60000, -0\.30000\b"), timeout=20_000)
    expect(marker).to_have_attribute("data-trail", "0")
    page.wait_for_timeout(1_000)
    expect(marker).to_have_attribute("data-trail", "0")
    with instance.client() as api:
        assert len(api.get(f"/api/devices/{gps_device.label}/trail").json()["fixes"]) >= 3
    assert offline.blocked == []
