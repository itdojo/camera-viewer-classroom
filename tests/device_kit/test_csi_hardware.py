"""A CSI-camera Device built from the kit, added to a local instance.

Two ways to run it.

**Against real hardware** (what closes the CSI kit's hardware checks): set
``CV_HW_CSI_ADDRESSES`` to the Device, one or more addresses separated by
commas. Run it once against a Pi 5 and once against a Pi 4, each running
``device-kit/csi`` as a student starts it, with a camera module on its ribbon;
take the rest from ``gen-cert.sh``'s output:

    CV_HW_CSI_ADDRESSES=192.0.2.70 \\
    CV_HW_CSI_PASSWORD=<password> CV_HW_CSI_FINGERPRINT=<fingerprint> \\
    uv run pytest tests/device_kit/test_csi_hardware.py -s

``CV_HW_CSI_USER`` defaults to ``viewer``. The instance runs on this machine
and must reach the Device's 8322/tcp and 9997/tcp, and the two clocks must
agree within an hour (RTSPS strict refuses the Relay's pull beyond that).
The Pi 5 has no H.264 encoder, so its Feed is encoded in software: the frame
rate check is what says the encoder keeps up at 1080p30.

**Against the CSI stand-in** (the default, when ``CV_HW_CSI_ADDRESSES`` is
unset): the harness starts a TEST STAND-IN, the CSI kit with an ffmpeg test
pattern encoded to the kit's boot profile, Quality Medium (1080p15 at
1.5 Mbit/s), in place of the camera (``tests/harness/csi-stand-in.yaml``).
Its Quality can't be changed, so it is measured at 15 fps rather than High's
30. That run proves the instance side handles a CSI-shaped 1080p Feed. It does
not prove ``rpiCamera`` works on a Pi, nor that the Pi 5's software encoder
keeps up.

Either way ``-s`` prints the measured frame rate and what the Feed cost the
instance in CPU.
"""

import os
import re
import secrets
import threading
import time

import pytest

ADDRESSES = [a.strip() for a in os.environ.get("CV_HW_CSI_ADDRESSES", "").split(",") if a.strip()]
STAND_IN = "stand-in"

CAMERA = "csi"
PLAYING = """() => {
    const video = document.querySelector('[aria-label="Single View"] video');
    return video && video.videoWidth > 0 && video.currentTime > 1;
}"""
NAME = re.compile(r"^(?P<stem>.+)-\d{8}-\d{6}-(?P<width>\d+)x(?P<height>\d+)\.mp4$")
MEASURE_SECONDS = 5


@pytest.fixture(scope="module")
def stand_in_device(device_harness):
    """The stand-in, or None against real hardware. ``csi_camera`` requests it
    directly, so it starts before ``no_leaked_devices`` snapshots the Devices
    and is not removed as one the first test leaked."""
    if ADDRESSES:
        yield None
        return
    device = device_harness.add_device(f"csi-si-{secrets.token_hex(2)}", variant="csi-stand-in")
    yield device
    device_harness.remove_device(device)


def _device_fields(address, device):
    """What a student enters in the Add Device form: the real Device's from
    the environment, or the stand-in's from the harness."""
    if address != STAND_IN:
        return f"hw-{secrets.token_hex(3)}", {
            "address": address,
            "username": os.environ.get("CV_HW_CSI_USER", "viewer"),
            "password": os.environ["CV_HW_CSI_PASSWORD"],
            "fingerprint": os.environ["CV_HW_CSI_FINGERPRINT"],
        }
    return f"stand-in-{secrets.token_hex(3)}", {
        "address": device.address,
        "username": device.username,
        "password": device.password,
        "fingerprint": device.fingerprint,
    }


@pytest.fixture(params=ADDRESSES or [STAND_IN])
def csi_camera(request, instance, stand_in_device):
    """The Device added at one of its addresses under a fresh label; yields
    the label, the Camera id and the frame rate it encodes at."""
    label, fields = _device_fields(request.param, stand_in_device)
    with instance.client(timeout=30) as api:
        response = api.post("/api/devices", json={"label": label, **fields})
    assert response.status_code == 201, response.text
    assert response.json()["cameras"] == [f"{label}/{CAMERA}"]
    fps = 15  # the kit boots at Quality Medium, 1080p15, and so does the stand-in
    if request.param != STAND_IN:
        # What the hardware checks measure is High, 1080p30 at 4 Mbit/s. The stand-in's ffmpeg
        # pattern reads as an ffmpeg kit, so it stays at Medium: High there is the USB ladder's.
        with instance.client(timeout=30) as api:
            high = api.put(f"/api/cameras/{label}/{CAMERA}/quality", json={"mode": "high"})
        assert high.status_code == 200, high.text
        fps = 30
    yield label, f"{label}/{CAMERA}", fps
    with instance.client() as api:
        api.delete(f"/api/devices/{label}")


def test_the_csi_feed_plays_in_a_browser_at_1920x1080_and_keeps_up_with_its_frame_rate(
        page, instance, csi_camera):
    label, _, wanted = csi_camera

    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(label)).click()
    page.wait_for_function(PLAYING, timeout=30_000)

    size = page.evaluate("""() => {
        const video = document.querySelector('[aria-label="Single View"] video');
        return [video.videoWidth, video.videoHeight];
    }""")
    samples = []
    sampler = threading.Thread(target=lambda: samples.append(instance.docker_cpu_percent()))
    cpu_before, clock_before = instance.cpu_seconds(), time.monotonic()
    sampler.start()
    frames = page.evaluate(f"""async () => {{
        const video = document.querySelector('[aria-label="Single View"] video');
        const before = video.getVideoPlaybackQuality().totalVideoFrames;
        await new Promise(resolve => setTimeout(resolve, {MEASURE_SECONDS * 1000}));
        return video.getVideoPlaybackQuality().totalVideoFrames - before;
    }}""")
    cpu_after, elapsed = instance.cpu_seconds(), time.monotonic() - clock_before
    sampler.join()
    fps = frames / MEASURE_SECONDS
    cpu = {name: 100 * (cpu_after[name] - cpu_before.get(name, 0)) / elapsed for name in cpu_after}
    print(f"\n{label}: {size[0]}x{size[1]}, {fps:.1f} fps decoded in Chromium; instance CPU "
          f"(% of one core) Relay {cpu.get('mediamtx', 0):.1f}, backend {cpu.get('python', 0):.1f}, "
          f"docker stats {samples[0] if samples else float('nan'):.1f}")
    assert size == [1920, 1080]
    assert fps >= 0.9 * wanted, f"the Device is not keeping up with {wanted} fps (or this machine's decoder is not)"


def test_a_recording_from_the_csi_camera_is_1920x1080_and_named_for_it(instance, csi_camera):
    label, camera, _ = csi_camera

    with instance.client(timeout=30) as api:
        response = api.post("/api/recordings", json={"camera": camera})
        assert response.status_code == 201, response.text
        time.sleep(6)
        stopped = api.post(f"/api/recordings/{response.json()['id']}/stop").json()

    assert stopped["state"] == "finalized", stopped
    [name] = stopped["captures"]
    match = NAME.match(name)
    assert match and match["stem"] == f"{label}-{CAMERA}", name
    assert (match["width"], match["height"]) == ("1920", "1080")
    probe = instance.probe_capture(name)
    video = [s for s in probe["streams"] if s["codec_type"] == "video"]
    print(f"\n{name}: {probe['format']['duration']} s, {video[0].get('avg_frame_rate')} fps")
    assert [(s["codec_name"], s["width"], s["height"]) for s in video] == [("h264", 1920, 1080)]
