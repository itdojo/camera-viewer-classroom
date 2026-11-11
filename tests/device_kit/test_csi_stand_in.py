"""The CSI stand-in: a test-only Device shaped like a Pi CSI camera Device.

TEST STAND-IN, not a kit variant. It runs the CSI kit's own compose file and
MediaMTX config (same hardening, same Camera path ``csi``), with two
overrides from ``tests/harness/csi-stand-in.yaml``: the ``-ffmpeg`` MediaMTX
image instead of ``-rpi``, and an ffmpeg test pattern published to ``csi``
instead of the ``rpiCamera`` source, encoded the way the kit boots the Pi, at
Quality Medium: 1920x1080, 15 fps, about 1.5 Mbit/s H.264, a keyframe every 15
frames, no B-frames.

What it proves is the instance side: that a CSI-shaped 1080p Feed adds,
plays and records. It proves nothing about ``rpiCamera`` on a Pi 4 or 5, or
whether the Pi 5's software encoder keeps up; that stays with
``test_csi_hardware.py`` run against real hardware.
"""

import json
from itertools import pairwise

import pytest

CAMERA = "csi"
SECONDS = 4


@pytest.fixture(scope="module")
def stand_in(device_harness):
    device = device_harness.add_device("csi-stand-in", variant="csi-stand-in")
    yield device
    device_harness.remove_device(device)  # its 1080p encode is not free


def frames(device_harness, device):
    """ffprobe the Camera from the session network for a few seconds."""
    result = device_harness.probe(
        "-v", "error", "-rtsp_transport", "tcp", "-read_intervals", f"%+{SECONDS + 1}",
        "-select_streams", "v:0", "-show_entries",
        "stream=codec_name,width,height,r_frame_rate:frame=pict_type,key_frame,pkt_size,pts_time,best_effort_timestamp_time",
        "-of", "json", device.rtsps_url(CAMERA))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_stand_in_publishes_csi_as_h264_1920x1080_at_15_fps(device_harness, stand_in):
    probe = frames(device_harness, stand_in)

    [stream] = probe["streams"]
    assert (stream["codec_name"], stream["width"], stream["height"]) == ("h264", 1920, 1080)
    assert stream["r_frame_rate"] == "15/1"


def test_the_stand_in_encodes_like_the_kit_asks_of_the_pi(device_harness, stand_in):
    probe = frames(device_harness, stand_in)

    video = probe["frames"]
    keyframes = [i for i, frame in enumerate(video) if frame["key_frame"] == 1]
    gaps = {b - a for a, b in pairwise(keyframes)}
    # ffprobe leaves out a time it doesn't know, and some frames carry neither (CI, 2026-10-15),
    # so the rate is taken over the frames from the first timed one to the last.
    timed = [(i, float(t)) for i, frame in enumerate(video)
             if (t := frame.get("pts_time", frame.get("best_effort_timestamp_time"))) is not None]
    (first, start), (last, end) = timed[0], timed[-1]
    seconds = end - start
    mbps = sum(int(frame["pkt_size"]) for frame in video[first + 1:last + 1]) * 8 / seconds / 1e6
    print(f"\n{len(video)} frames over {seconds:.1f} s, keyframes every {gaps}, {mbps:.2f} Mbit/s")
    assert "B" not in {frame["pict_type"] for frame in video}  # WebRTC needs none
    assert len(keyframes) >= 3 and gaps == {15}
    assert 1.1 <= mbps <= 1.75  # 1.5 Mbit/s target, like Medium's rpiCameraBitrate


def test_the_stand_in_keeps_the_csi_kits_hardening(device_harness, stand_in):
    # The loopback publisher's new permission must not let anyone else in.
    publish = device_harness.run_in_network(
        "bluenviron/mediamtx:1.21.2-ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=1",
        "-c:v", "libx264", "-f", "rtsp", "-rtsp_transport", "tcp", stand_in.rtsps_url(CAMERA, credentials=False),
        entrypoint="ffmpeg")
    unauthenticated = device_harness.probe("-v", "error", "-rtsp_transport", "tcp",
                                           stand_in.rtsps_url(CAMERA, credentials=False))
    plain = device_harness.probe("-v", "error", "-rtsp_transport", "tcp",
                                 stand_in.rtsps_url(CAMERA, scheme="rtsp", port=8554))
    with stand_in.api_client(auth=False) as api:
        api_status = api.get("/v3/paths/list").status_code

    assert publish.returncode != 0 and "401" in publish.stderr, publish.stderr
    assert unauthenticated.returncode != 0 and "401" in unauthenticated.stderr
    assert plain.returncode != 0 and "Connection refused" in plain.stderr
    assert api_status == 401
