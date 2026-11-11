"""Seam B: Single View brings its Device's Position at 1 Hz (#64), and Map View brings every
GPS Device's (#78, ADR 0012).

The shared instance runs the default timings, so an unwatched Device streams one fix every 30
seconds. Opening one of its Cameras in Single View, or opening Map View, makes the page renew
Position Demand at once and every 10 seconds after, and the fixes, as ``positions`` events carry
them, come a second apart. No test renews over the API, so a fast fix proves the page renewed.
The browser can reach the instance and nothing else (``harness.offline``).
"""

import itertools
import re
import time

import pytest
from playwright.sync_api import Page, expect

from harness.gps import Epoch, track_lines, write_track

TRACK = "still.nmea"
EPOCH = Epoch("101500.00", 52.37, 4.89, 2.0, 0.0, 0.0)
NORTH = [Epoch(f"1015{second:02d}.00", 52.37 + second * 20 / 111_320, 4.89, 2.0, 20.0, 0.0)
         for second in range(60)]
RENEW_SECONDS = 10


@pytest.fixture
def gps_device(instance, device_harness, unique_label, tmp_path):
    tracks = write_track(tmp_path / "tracks", track_lines([EPOCH]), TRACK)
    device = device_harness.add_device(unique_label("rate"), gps=True, gps_tracks=tracks,
                                       settings={"GPS_SOURCE": f"replay:{TRACK}"})
    instance.add_device(device)
    yield device
    with instance.client() as api:
        api.delete(f"/api/devices/{device.label}")


@pytest.fixture
def joining_device(instance, device_harness, unique_label, tmp_path):
    """Call it to bring up a GPS Device heading north and add it to the shared instance, so it
    joins a Map View already open. Returns its label."""
    added = []

    def join() -> str:
        tracks = write_track(tmp_path / "north", track_lines(NORTH), "north.nmea")
        device = device_harness.add_device(unique_label("maprate"), gps=True, gps_tracks=tracks,
                                           settings={"GPS_SOURCE": "replay:north.nmea"})
        added.append(device)
        instance.add_device(device)
        return device.label

    yield join
    with instance.client() as api:
        for device in added:
            api.delete(f"/api/devices/{device.label}")
    for device in added:
        device_harness.remove_device(device)  # it encodes 720p in software


@pytest.fixture
def plain_device(instance, test_pattern_device, unique_label):
    """The label of a Device with no GPS Sidecar on the shared instance."""
    label = unique_label("nogps")
    instance.add_device(test_pattern_device, label=label)
    yield label
    with instance.client() as api:
        api.delete(f"/api/devices/{label}")


def next_fix(stream, label, timeout):
    """Wait for ``label``'s next accepted fix: a Position published with an age of 0, not one
    re-sent with another Device's fix. Returns when it arrived."""
    stream.next("positions", lambda p: label in p and p[label]["age"] < 0.5, timeout=timeout)
    return time.monotonic()


RENEWALS = """label => performance.getEntriesByType('resource')
    .filter((entry) => entry.name.endsWith(`/api/devices/${label}/position-demand`))
    .map((entry) => (performance.timeOrigin + entry.startTime) / 1000)"""


def renewals(page, label):
    """When the page sent each renewal of ``label``'s Position Demand, in seconds since the epoch."""
    return page.evaluate(RENEWALS, label)


def is_renewal(label):
    return lambda response: (response.request.method == "PUT"
                             and response.url.endswith(f"/api/devices/{label}/position-demand"))


def test_single_view_renews_position_demand_and_brings_fixes_at_1_hz(offline, instance, gps_device):
    label = gps_device.label
    page = offline.new_page()

    with instance.events() as stream:
        next_fix(stream, label, timeout=15)  # the first fix of the idle stream comes at once
        page.goto(instance.base_url)
        with pytest.raises(TimeoutError):  # Multi View wants no fast rate: the next is 30 s away
            next_fix(stream, label, timeout=4)
        assert renewals(page, label) == []

        # The offline guard routes every request through Playwright, which serves routes only
        # while the test is inside a Playwright call: wait for the renewal's answer in one.
        with page.expect_response(is_renewal(label)) as answer:
            page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(rf"\b{label}\b")).click()
            opened, opened_at = time.monotonic(), time.time()
        assert answer.value.status == 204
        fixes = [next_fix(stream, label, timeout=5) for _ in range(4)]

    assert fixes[0] - opened < 3  # within a second or two of opening (story 42)
    assert all(later - earlier < 1.6 for earlier, later in itertools.pairwise(fixes)), fixes
    first = renewals(page, label)[0]
    assert first - opened_at < 1

    with page.expect_response(is_renewal(label), timeout=(RENEW_SECONDS + 3) * 1000):
        pass
    second = renewals(page, label)[1]
    assert RENEW_SECONDS - 1 < second - first < RENEW_SECONDS + 1
    assert offline.blocked == []


def switch_to(page: Page, view: str) -> None:
    page.get_by_role("banner").get_by_role("group", name="View").get_by_role("button", name=view).click()


def map_marker(page: Page, label: str):
    return page.get_by_role("region", name="Map View").get_by_role("button", name=re.compile(rf"^{label}(, \S+)?: "))


def marker_names(page: Page, label: str, seconds: float) -> set[str]:
    """The names ``label``'s Map View marker went through, read four times a second."""
    names = set()
    for _ in range(int(seconds * 4)):
        names.add(map_marker(page, label).get_attribute("aria-label"))
        page.wait_for_timeout(250)
    return names


def test_map_view_renews_position_demand_for_every_gps_device_and_leaving_sends_nothing(
        offline, instance, joining_device, plain_device):
    page = offline.new_page()
    sent = []
    page.on("request", lambda request: (re.search(r"/api/devices/[^/]+/position-demand$", request.url)
                                        and sent.append(request.method)))
    page.goto(instance.base_url)
    switch_to(page, "Map View")
    expect(page.get_by_role("region", name="Map View")).to_be_visible()

    with instance.events() as stream:
        label = joining_device()  # a GPS Device joins the open Map View
        with page.expect_response(is_renewal(label), timeout=20_000) as answer:
            pass
        assert answer.value.status == 204
        # The idle stream's first fix may come just before the fast rate starts; the next is
        # 30 s away unless the page's renewal took, so timing out here means no fast rate.
        fixes = [next_fix(stream, label, timeout=5) for _ in range(5)]
    assert all(later - earlier < 1.6 for earlier, later in itertools.pairwise(fixes[1:])), fixes

    assert len(marker_names(page, label, seconds=4)) >= 3  # the marker moves about once a second
    assert renewals(page, plain_device) == []  # a Device with no GPS Sidecar has no fix to speed up

    switch_to(page, "Multi View")
    before = len(sent)
    page.wait_for_timeout((RENEW_SECONDS + 2) * 1000)
    assert sent[before:] == []  # no release and no renewal: the Demand lingers out by itself
    assert set(sent) == {"PUT"}
    assert offline.blocked == []
