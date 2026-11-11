"""Throttling a Device's uplink, so Feed Status and Auto Quality meet real congestion (#48).

``Device.throttle`` shapes everything the Device sends (``tc``: ``tbf`` for
rate, ``netem`` for delay and loss) from a throwaway sidecar in the Device's
network namespace. The sidecar holds ``NET_ADMIN``; the Device, and so every
shipped kit, holds no capability for it.
"""

import json
import subprocess
import time

import httpx
import pytest

from harness.devices import KIT_DIR


@pytest.fixture
def device(device_harness, unique_label):
    return device_harness.add_device(unique_label("tc"))


def timed_get(api, path="/v3/info"):
    start = time.monotonic()
    api.get(path).raise_for_status()
    return time.monotonic() - start


def test_a_delay_slows_every_answer_until_unthrottled(device):
    with device.api_client() as api:
        timed_get(api)  # the TLS handshake, out of the way
        device.throttle(delay_ms=400)
        delayed = timed_get(api)
        device.unthrottle()
        clear = timed_get(api)

    assert delayed >= 0.4
    assert clear < 0.4


def test_total_loss_cuts_the_device_off_until_unthrottled(device):
    device.throttle(loss_percent=100)
    with pytest.raises(httpx.TimeoutException), device.api_client(timeout=2) as api:
        api.get("/v3/info")

    device.unthrottle()
    with device.api_client() as api:
        assert api.get("/v3/info").status_code == 200


def test_unthrottling_an_unthrottled_device_is_harmless(device):
    device.unthrottle()
    with device.api_client() as api:
        assert api.get("/v3/info").status_code == 200


def device_discarded(device):
    """Packets the Device dropped from its readers' write queues ("reader is too slow")."""
    with device.api_client() as api:
        sessions = api.get("/v3/rtspssessions/list").json()["items"]
    return sum(session.get("outboundRTPPacketsDiscarded", 0) for session in sessions)


def relay_rtp(instance, camera_id):
    """The Relay's static-source RTP counters for a Camera: (received, lost)."""
    source = instance.relay_get(f"/v3/paths/static-sources/get/{camera_id}") or {}
    rtp = source.get("typeSpecific") or {}
    return rtp.get("inboundRTPPackets", 0), rtp.get("inboundRTPPacketsLost", 0)


def wait_until(predicate, timeout, interval=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_an_uplink_too_slow_for_the_feed_shows_as_device_discards_and_relay_loss(
        instance, device_harness, unique_label):
    """The loss signal ADR 0008 and 0009 rest on: over the RTSPS/TCP pull, a
    2 Mbps test pattern (about 1.7 Mbps on the wire) behind an 800 kbit uplink
    overflows the Device's write queue, and the Relay counts the gaps as lost
    RTP packets.

    The Relay sees a gap only once everything queued ahead of it has drained
    through the throttle, so its loss trails the Device's discards. Measured
    on 2026-10-15 (Docker Desktop, 5 runs): discards from 3.9 to 4.9 s after
    the throttle, Relay loss from 9.2 to 11.9 s. Both delays scale with the
    Feed's packet rate, and a test pattern slowed by a busy CI runner took
    11 s to its first discard (run 39090827659) and over 10 s again on
    run 39528900126, so the first gets 30 s. The second gets twice the
    measured discard delay plus 5 s, and never less than 20 s from the
    throttle."""
    device = device_harness.add_device(unique_label("slow"))
    label = unique_label("slow")
    camera_id = f"{label}/test-pattern"
    instance.add_device(device, label=label)

    with instance.client() as api:
        recording = api.post("/api/recordings", json={"camera": camera_id})  # holds the pull (ADR 0004)
        assert recording.status_code == 201, recording.text
        try:
            assert wait_until(lambda: relay_rtp(instance, camera_id)[0] > 0, timeout=20)
            discarded, (_, lost) = device_discarded(device), relay_rtp(instance, camera_id)

            device.throttle(kbit=800)
            throttled = time.monotonic()
            assert wait_until(lambda: device_discarded(device) > discarded, timeout=30), (
                f"Device discarded nothing new: {discarded} -> {device_discarded(device)}")
            to_discard = time.monotonic() - throttled
            remaining = max(20 - to_discard, 2 * to_discard + 5)
            assert wait_until(lambda: relay_rtp(instance, camera_id)[1] > lost, timeout=remaining), (
                f"Relay lost nothing new: {lost} -> {relay_rtp(instance, camera_id)[1]}")
        finally:
            device.unthrottle()
            api.post(f"/api/recordings/{recording.json()['id']}/stop")


@pytest.mark.parametrize("kit", ["test-pattern", "usb", "csi"])
def test_the_shipped_kits_render_with_no_net_admin(kit, tmp_path):
    """Rendering needs only the compose CLI, so this stays in the fast tier."""
    env = tmp_path / ".env"
    env.write_text("INSTANCE_USER=u\nINSTANCE_PASS=p\nUSB_CAMERA=/dev/null\n")
    rendered = subprocess.run(
        ["docker", "compose", "--env-file", str(env), "-f", str(KIT_DIR / kit / "compose.yaml"),
         "config", "--format", "json"], capture_output=True, text=True, timeout=60, check=True)

    for name, service in json.loads(rendered.stdout)["services"].items():
        assert "NET_ADMIN" not in service.get("cap_add", []), name
        assert not service.get("privileged"), name
