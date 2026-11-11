"""A real USB-webcam Device built from the kit, added to a local instance.

Opt-in, because it needs hardware: skipped unless ``CV_HW_USB_ADDRESSES``
names the Device, one or more addresses separated by commas (say its Ethernet
and its WireGuard address). The Device runs ``device-kit/usb`` as a student
starts it; take the rest from ``gen-cert.sh``'s output:

    CV_HW_USB_ADDRESSES=192.0.2.62,192.0.2.40 \\
    CV_HW_USB_PASSWORD=<password> CV_HW_USB_FINGERPRINT=<fingerprint> \\
    uv run pytest tests/device_kit/test_usb_hardware.py

``CV_HW_USB_USER`` defaults to ``viewer``. The instance runs on this machine
and must reach the Device's 8322/tcp and 9997/tcp at each address, and the
two clocks must agree within an hour: RTSPS strict keys SRTP with a
timestamped MIKEY message, and the Device refuses the Relay's pull ("NTP
difference is too high") beyond that.
"""

import os
import re
import secrets
import time

import pytest

ADDRESSES = [a.strip() for a in os.environ.get("CV_HW_USB_ADDRESSES", "").split(",") if a.strip()]
pytestmark = pytest.mark.skipif(not ADDRESSES, reason="no USB Device: set CV_HW_USB_ADDRESSES")

CAMERA = "usb"
PLAYING = """() => {
    const video = document.querySelector('[aria-label="Single View"] video');
    return video && video.videoWidth > 0 && video.currentTime > 1;
}"""
NAME = re.compile(r"^(?P<stem>.+)-\d{8}-\d{6}-(?P<width>\d+)x(?P<height>\d+)\.mp4$")


@pytest.fixture(params=ADDRESSES)
def usb_camera(request, instance):
    """The Device added at one of its addresses under a fresh label; yields
    the label and the Camera id."""
    label = f"hw-{secrets.token_hex(3)}"
    with instance.client(timeout=30) as api:
        response = api.post("/api/devices", json={
            "address": request.param,
            "label": label,
            "username": os.environ.get("CV_HW_USB_USER", "viewer"),
            "password": os.environ["CV_HW_USB_PASSWORD"],
            "fingerprint": os.environ["CV_HW_USB_FINGERPRINT"],
        })
    assert response.status_code == 201, response.text
    assert response.json()["cameras"] == [f"{label}/{CAMERA}"]
    # The kit boots at Quality Medium (15 fps); these checks measure High, 720p30 at 2 Mbit/s.
    with instance.client(timeout=30) as api:
        high = api.put(f"/api/cameras/{label}/{CAMERA}/quality", json={"mode": "high"})
    assert high.status_code == 200, high.text
    yield label, f"{label}/{CAMERA}"
    with instance.client() as api:
        api.delete(f"/api/devices/{label}")


def test_the_usb_feed_plays_in_a_browser_at_1280x720(page, instance, usb_camera):
    label, _ = usb_camera

    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(label)).click()
    page.wait_for_function(PLAYING, timeout=30_000)

    size = page.evaluate("""() => {
        const video = document.querySelector('[aria-label="Single View"] video');
        return [video.videoWidth, video.videoHeight];
    }""")
    frames = page.evaluate("""async () => {
        const video = document.querySelector('[aria-label="Single View"] video');
        const before = video.getVideoPlaybackQuality().totalVideoFrames;
        await new Promise(resolve => setTimeout(resolve, 2000));
        return video.getVideoPlaybackQuality().totalVideoFrames - before;
    }""")
    print(f"\n{label}: {size[0]}x{size[1]}, {frames / 2:.1f} fps decoded in Chromium")
    assert size == [1280, 720]
    assert frames >= 2 * 20  # 30 fps at the Device; allow for a slow decoder


def test_a_recording_from_the_usb_camera_is_1280x720_and_named_for_it(instance, usb_camera):
    label, camera = usb_camera

    with instance.client(timeout=30) as api:
        response = api.post("/api/recordings", json={"camera": camera})
        assert response.status_code == 201, response.text
        time.sleep(6)
        stopped = api.post(f"/api/recordings/{response.json()['id']}/stop").json()

    assert stopped["state"] == "finalized", stopped
    [name] = stopped["captures"]
    match = NAME.match(name)
    assert match and match["stem"] == f"{label}-{CAMERA}", name
    assert (match["width"], match["height"]) == ("1280", "720")
    probe = instance.probe_capture(name)
    print(f"\n{name}: {probe['format']['duration']} s")
    video = [s for s in probe["streams"] if s["codec_type"] == "video"]
    assert [(s["codec_name"], s["width"], s["height"]) for s in video] == [("h264", 1280, 720)]
