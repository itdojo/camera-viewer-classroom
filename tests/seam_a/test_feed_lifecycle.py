"""Seam A: Feed Status, as the Camera list and the event stream report it.

A Feed needs a viewer to be pulled, and the viewer is a real browser with the Camera open in
Single View (Playwright's ``page``): it keeps the Feed on screen, and re-opens it after a drop,
exactly as a student's tab would. Every assertion is still on the API's event stream.
"""

import re
import time


def camera_view(cameras, camera_id):
    return next(c for c in cameras if c["id"] == camera_id)


def status_is(camera_id, expected):
    return lambda cameras: any(c["id"] == camera_id and c.get("feed_status") == expected for c in cameras)


def test_a_camera_nobody_is_watching_is_idle(instance, test_pattern_device, unique_label):
    label = unique_label("idle")
    instance.add_device(test_pattern_device, label=label)

    with instance.client() as api:
        view = camera_view(api.get("/api/cameras").json(), f"{label}/test-pattern")
    assert view["feed_status"] == "idle"
    assert view["feed_status_reason"] is None


def test_stopping_a_device_makes_its_feeds_offline_and_starting_it_brings_them_back(instance, own_device):
    device = own_device("stop")
    camera = f"{device.label}/test-pattern"

    with instance.events() as events:
        device.stop()
        events.next("cameras", where=status_is(camera, "offline"), timeout=30)
        device.start()
        events.next("cameras", where=status_is(camera, "idle"), timeout=30)


def test_a_rebuilt_device_shows_down_with_reason_certificate_changed(instance, own_device):
    device = own_device("rebuilt")
    camera = f"{device.label}/test-pattern"

    with instance.events() as events:
        device.regenerate_cert()
        cameras = events.next("cameras", where=status_is(camera, "down"), timeout=30)

    assert camera_view(cameras, camera)["feed_status_reason"] == "certificate changed"


def test_a_recording_feed_is_live_with_no_browser_watching(instance, test_pattern_device, unique_label):
    label = unique_label("rec-live")
    instance.add_device(test_pattern_device, label=label)
    camera = f"{label}/test-pattern"

    with instance.events() as events, instance.client() as api:
        response = api.post("/api/recordings", json={"camera": camera})
        assert response.status_code == 201, response.text
        try:
            events.next("cameras", where=status_is(camera, "live"), timeout=30)
        finally:
            api.post(f"/api/recordings/{response.json()['id']}/stop")


def watch(page, instance, label):
    """Open the Device's Camera in Single View and keep it on screen."""
    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(label)).click()


def test_a_watched_feed_is_live(page, instance, test_pattern_device, unique_label):
    label = unique_label("live")
    instance.add_device(test_pattern_device, label=label)

    with instance.events() as events:
        watch(page, instance, label)
        events.next("cameras", where=status_is(f"{label}/test-pattern", "live"), timeout=30)


def test_pausing_a_watched_device_drives_its_feed_down_and_unpausing_brings_it_back(
        page, instance, own_device):
    device = own_device("pause")
    camera = f"{device.label}/test-pattern"

    with instance.events() as events:
        watch(page, instance, device.label)
        events.next("cameras", where=status_is(camera, "live"), timeout=30)
        device.pause()
        paused = time.monotonic()
        try:
            cameras = events.next("cameras", where=status_is(camera, "down"), timeout=45)
            down_after = time.monotonic() - paused
        finally:
            device.unpause()
        assert camera_view(cameras, camera)["feed_status_reason"] is None
        # Down 5 s (FEED_DOWN_AFTER_SECONDS) after the Feed stops arriving, give or take
        # the 1 s sampling: not after the Relay's own 10 s read timeout on top.
        assert 4 <= down_after <= 8, down_after
        events.next("cameras", where=status_is(camera, "live"), timeout=45)


def test_a_watched_feed_whose_device_stops_is_offline_and_live_again_once_it_restarts(
        page, instance, own_device):
    device = own_device("restart")
    camera = f"{device.label}/test-pattern"

    with instance.events() as events:
        watch(page, instance, device.label)
        events.next("cameras", where=status_is(camera, "live"), timeout=30)
        device.stop()
        events.next("cameras", where=status_is(camera, "offline"), timeout=30)
        device.start()
        events.next("cameras", where=status_is(camera, "live"), timeout=45)


def relay_counters(instance, camera_id):
    """What the Relay has counted on a Camera's pull: RTP packets lost on its static source and
    errored frames on its path. Read only to show why a Feed changed, never to decide it."""
    source = instance.relay_get(f"/v3/paths/static-sources/get/{camera_id}") or {}
    path = instance.relay_get(f"/v3/paths/get/{camera_id}") or {}
    return {"lost": (source.get("typeSpecific") or {}).get("inboundRTPPacketsLost", 0),
            "errored_frames": path.get("inboundFramesInError", 0)}


DEVICE_WRITE_QUEUE = 512  # packets: MediaMTX's writeQueueSize, which the kits leave at its default


def feed_rate(instance, camera_id, seconds=3.0):
    """What a pulled Feed carries now, from the Relay over ``seconds`` (whole GOPs of the test
    pattern, an IDR a second): kbit/s on the wire and RTP packets per second."""
    def read():
        source = instance.relay_get(f"/v3/paths/static-sources/get/{camera_id}") or {}
        path = instance.relay_get(f"/v3/paths/get/{camera_id}") or {}
        return (time.monotonic(), path.get("inboundBytes", 0),
                (source.get("typeSpecific") or {}).get("inboundRTPPackets", 0))

    start, bytes_before, packets_before = read()
    time.sleep(seconds)
    end, bytes_after, packets_after = read()
    return (bytes_after - bytes_before) * 8 / (end - start) / 1000, (packets_after - packets_before) / (end - start)


def test_a_feed_whose_device_uplink_is_too_slow_reads_degraded_from_loss_and_live_once_freed(
        instance, own_device):
    """A test pattern behind an uplink at half what its Feed carries (about 960 kbit for the
    2 Mbps pattern, about 1.9 Mbps and 250 RTP packets a second on the wire). The Device
    discards from its write queue, and the Relay counts the gaps as lost RTP packets about 10 s
    later, once what was queued ahead of them has drained (#48). Degraded then needs that loss
    to pass 1% of the 10 s window, so the Feed gets at least 30 s. The source keeps arriving throughout,
    only short, so the Feed never reads Down or Offline on the way.

    Both the uplink and that deadline come from the Feed as measured, because the pattern is a
    software encode that falls behind real time on a busy host, as on a 4-vCPU CI runner (run
    38813799261: no Relay loss within 30 s of a fixed 800 kbit, and Recording drift resets, the
    mark of a late encoder). A late encoder sends fewer, smaller packets; near 800 kbit nothing
    congests, and at any rate the gap reaches the Relay only once the Device's 512-packet write
    queue has filled at the excess and drained through the uplink. At half the Feed both take
    512 / (packets a second / 2), so the deadline is twice that plus the window, or 30 s if more.
    Measured with the Device capped at 0.12 to 0.13 CPU (about 60 packets a second, where the
    model says 34 s): Relay loss from 40 s, Degraded at 50 to 56 s, and a fixed 800 kbit failed.

    Before the loss arrives, TCP stalls behind the throttle leave the Relay up to a second with
    no new bytes, and a sample taken then reads Degraded for that sample alone (a failing source,
    ADR 0008). So the test waits for a Degraded that loss holds: the Relay has counted loss and
    no errored frames, and the Feed still reads Degraded 3 s later, longer than any stall lasted.

    A Recording holds the pull (ADR 0004), as in #48's loss test, so no browser is needed. The
    Camera list is polled rather than the event stream followed, because a stall's Degraded can
    run straight into loss's with no event between them. The Relay counters cost two ``docker
    exec`` each, on the CPUs the Device encodes on under Linux, so they are read only while the
    Feed reads Degraded, the only time they decide anything."""
    device = own_device("congested")
    camera = f"{device.label}/test-pattern"
    seen = []
    sent = 0

    def watch(api, until, timeout):
        """Read the Feed from the Camera list until ``until(view, counters)``, recording each
        Feed Status it passes through. Returns the seconds taken, the view and the counters."""
        start = time.monotonic()
        while True:
            view = camera_view(api.get("/api/cameras").json(), camera)
            counters = relay_counters(instance, camera) if view["feed_status"] == "degraded" else None
            if seen[-1:] != [view["feed_status"]]:
                seen.append(view["feed_status"])
            if until(view, counters):
                return time.monotonic() - start, view, counters
            if time.monotonic() - start >= timeout:
                raise AssertionError(f"not within {timeout:.0f} s: {seen}, Relay "
                                     f"{counters or relay_counters(instance, camera)}, Feed {sent:.0f} kbit/s")
            time.sleep(0.25)

    def degraded_by_loss(view, counters):
        return view["feed_status"] == "degraded" and counters["lost"] > before["lost"]

    with instance.client() as api:
        recording = api.post("/api/recordings", json={"camera": camera})
        assert recording.status_code == 201, recording.text
        try:
            watch(api, lambda view, _: view["feed_status"] == "live", timeout=30)
            sent, packets = feed_rate(instance, camera)
            uplink = int(sent / 2)
            reaches_relay = 2 * DEVICE_WRITE_QUEUE / (packets / 2)
            before = relay_counters(instance, camera)
            seen[:] = ["live"]

            device.throttle(kbit=uplink)
            degraded_after, view, during = watch(api, degraded_by_loss, timeout=max(30, 2 * reaches_relay + 10))
            held = time.monotonic()
            watch(api, lambda v, _: v["feed_status"] != "degraded" or time.monotonic() - held >= 3, timeout=5)
            assert seen[-1] == "degraded", f"Degraded did not hold: {seen}"

            device.unthrottle()
            live_after, _, _ = watch(api, lambda v, _: v["feed_status"] == "live", timeout=30)
        finally:
            device.unthrottle()
            api.post(f"/api/recordings/{recording.json()['id']}/stop")

    print(f"Feed {sent:.0f} kbit/s, {packets:.0f} packets/s, uplink {uplink} kbit; degraded {degraded_after:.1f} s after throttle, live {live_after:.1f} s after unthrottle;"
          f" Relay {before} before, {during} at Degraded; statuses {seen}")
    assert view["feed_status_reason"] is None
    assert during["errored_frames"] == before["errored_frames"], (before, during)
    assert not {"down", "offline"} & set(seen), seen
