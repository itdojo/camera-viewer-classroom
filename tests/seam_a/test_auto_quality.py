"""Seam A: Auto Quality steps a congested Camera down until its Feed is Live, and back up once the
link clears (spec #46, ADR 0009; #49).

A test-pattern Device boots at High on the USB ladder (720p30 at 2M, about 1.7 Mbps on the wire)
and is throttled to 600 kbit, below Medium's 1 Mbps and above Low's 500k. Its Relay loss arrives
some 10 s after the throttle (#48), so even with this module's short ``QUALITY_*`` timings a step
down takes about 20 s. Every step on this kit restarts ffmpeg and drops the pull for about 5 s.

Medium can't carry 600 kbit either, but it takes a while to show: its excess over the link is
small, so the Device's write queue overflows, and the Relay counts loss, only some 18 s after the
Feed is back at Medium (measured 2026-10-15). So a level counts as holding only once its Feed has
stayed Live for 30 s; until then the test keeps watching Auto step down.

The timings are shortened through the instance's environment: 5 s windows, 10 s clean before a
step up that keeps viewers and 45 s before one that drops them (every step here), longer than the
30 s hold, so Auto can't try a step up on the still-throttled link first.

A browser in Single View holds the pull and re-opens the Feed after each step, as a student's tab
would, and shows the Feed's "<level> · auto" label, checked at every step. A Recording can't hold
the pull here: while Recording, Auto never steps up across a step that drops viewers, and on this
kit every step does.
"""

import re
import time
from itertools import pairwise

import pytest
from playwright.sync_api import expect

TIMINGS = {
    "CV_QUALITY_CHECK_SECONDS": "1",
    "QUALITY_WINDOW_SECONDS": "5",
    "QUALITY_UP_CLEAN_SECONDS": "10",
    "QUALITY_UP_TRIAL_SECONDS": "10",
    "QUALITY_UP_WAIT_MAX_SECONDS": "90",
    "QUALITY_UP_DISRUPTIVE_CLEAN_SECONDS": "45",
}
LADDER = ["minimum", "low", "medium", "high"]
WINDOW = float(TIMINGS["QUALITY_WINDOW_SECONDS"])


@pytest.fixture(scope="module")
def auto_instance(instances, device_harness):
    """This module's instance. ``device_harness`` is named so the instance starts before
    ``no_leaked_devices`` takes its snapshot."""
    instance = instances.start("auto", env=TIMINGS)
    yield instance
    instances.remove(instance)


@pytest.fixture
def congested(auto_instance, device_harness, unique_label):
    device = device_harness.add_device(unique_label("auto"))
    auto_instance.add_device(device)
    yield device, f"{device.label}/test-pattern"
    device.unthrottle()
    with auto_instance.client() as api:
        api.delete(f"/api/devices/{device.label}")
    device_harness.remove_device(device)


def level(view):
    return view["quality"]["level"]


def rank(view):
    return LADDER.index(level(view))


def test_auto_steps_a_throttled_camera_down_until_its_feed_is_live_and_back_up_once_freed(
        page, auto_instance, congested):
    device, camera = congested
    history = []  # (seconds since start, Feed Status, Quality mode, Quality level), on each change
    start = time.monotonic()

    def watch(api, until, timeout, what):
        deadline = time.monotonic() + timeout
        while True:
            view = next(c for c in api.get("/api/cameras").json() if c["id"] == camera)
            state = (view["feed_status"], view["quality"]["mode"], level(view))
            if not history or history[-1][1:] != state:
                if history and level(view) and level(view) != history[-1][3]:  # a step: the Feed says so
                    expect(header.get_by_text(f"{level(view).title()} · auto", exact=True).filter(visible=True)
                           ).to_be_visible(timeout=5_000)
                history.append((round(time.monotonic() - start, 1), *state))
            if until(view):
                return view
            assert time.monotonic() < deadline, f"{what} not within {timeout} s: {history}"
            time.sleep(0.5)

    def holds(stepped):
        """Whether the Feed stays Live at ``stepped``'s level for 30 s."""
        held_from = time.monotonic()
        watch(api, lambda v: level(v) != level(stepped) or v["feed_status"] != "live"
              or time.monotonic() - held_from >= 30, 35, "a held level")
        return history[-1][1:] == ("live", "auto", level(stepped))

    page.goto(auto_instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(device.label)).click()
    header = page.get_by_role("region", name="Single View").locator("header")
    with auto_instance.client(timeout=30) as api:
        try:
            watch(api, lambda v: v["feed_status"] == "live" and level(v) == "high", 30, "Live at High")

            device.throttle(kbit=600)
            watch(api, lambda v: level(v) != "high", 60, "a step down")
            deadline = time.monotonic() + 150
            while True:
                stepped = watch(api, lambda v: v["feed_status"] == "live" and rank(v) < LADDER.index("high"),
                                deadline - time.monotonic(), "Live below High")
                if holds(stepped):
                    break

            device.unthrottle()
            up = watch(api, lambda v: rank(v) > rank(stepped), 90, "a step back up")
            watch(api, lambda v: v["feed_status"] == "live" and level(v) == level(up), 30, "Live after the step up")
        finally:
            device.unthrottle()

    print(f"history: {history}")
    assert all(mode == "auto" for _, _, mode, _ in history), history
    assert not {"down", "offline"} & {status for _, status, _, _ in history}, history
    steps = [(at, lvl) for (_, _, _, was), (at, _, _, lvl) in pairwise(history) if was and lvl != was]
    levels = [next(lvl for *_, lvl in history if lvl)] + [lvl for _, lvl in steps]
    assert all(abs(LADDER.index(a) - LADDER.index(b)) == 1 for a, b in pairwise(levels)), \
        f"not one level a step: {history}"
    # A step's own restart (the pull drops for about 5 s) never sets off another: each step is
    # judged from the Feed's return, over at least two windows.
    assert all(b - a >= 2 * WINDOW for (a, _), (b, _) in pairwise(steps)), f"steps too close: {history}"


def test_auto_never_writes_over_another_instances_change_and_a_pick_takes_control_back(auto_instance, congested):
    """ADR 0009: the comparison with the last write runs before every write, so a change another
    instance makes while the Device stays reachable is caught at the next Auto step, which then
    writes nothing. The periodic check reads only ``started``, so only that step can catch it.

    The other instance moves the Device to 3M, off the ladder, and the uplink is throttled, so Auto
    soon wants to step down (from loss, or from the Feed going Down while ffmpeg restarts). A
    Recording holds the pull: Auto still steps down while Recording."""
    device, camera = congested
    with auto_instance.client(timeout=30) as api:
        recording = api.post("/api/recordings", json={"camera": camera})
        assert recording.status_code == 201, recording.text
        try:
            assert api.put(f"/api/cameras/{camera}/quality", json={"mode": "high"}).status_code == 200
            assert api.put(f"/api/cameras/{camera}/quality", json={"mode": "auto"}).status_code == 200
            with device.api_client() as other:
                command = other.get("/v3/config/paths/get/test-pattern").json()["runOnInit"]
                assert "-b:v 2M -maxrate 2M -bufsize 1M" in command
                theirs = command.replace("-b:v 2M -maxrate 2M -bufsize 1M", "-b:v 3M -maxrate 3M -bufsize 1500k")
                other.patch("/v3/config/paths/patch/test-pattern", json={"runOnInit": theirs}).raise_for_status()
            device.throttle(kbit=600)

            deadline = time.monotonic() + 60
            while not (view := next(c for c in api.get("/api/cameras").json() if c["id"] == camera)
                       )["quality"]["changed_elsewhere"]:
                assert time.monotonic() < deadline, f"no Auto step caught the change: {view['quality']}"
                time.sleep(0.5)
            device.unthrottle()
            assert view["quality"]["mode"] == "auto"
            with device.api_client() as other:
                assert other.get("/v3/config/paths/get/test-pattern").json()["runOnInit"] == theirs

            picked = api.put(f"/api/cameras/{camera}/quality", json={"mode": "low", "confirm": True})
            assert picked.status_code == 200, picked.text
            assert picked.json()["quality"]["changed_elsewhere"] is False
            with device.api_client() as other:
                assert "size=640x360" in other.get("/v3/config/paths/get/test-pattern").json()["runOnInit"]
        finally:
            device.unthrottle()
            api.post(f"/api/recordings/{recording.json()['id']}/stop")
