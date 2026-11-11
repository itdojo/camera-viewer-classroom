"""The USB Device Kit variant, without a webcam.

The kit runs exactly as a student starts it, but with /dev/null mapped in as
the camera: MediaMTX, its hardening and the configured capture command are all
real, only ffmpeg's input fails (and is retried). Playback from a real webcam
is verified by hand on the test Device cam2; see docs/working-notes.md.
"""

import json
import os
import re
import shlex
import subprocess
import time

import httpx
import pytest

from harness.devices import KIT_DIR

CAMERA = "usb"
IN_CONTAINER_CAMERA = "/dev/camera-usb"


@pytest.fixture(scope="module")
def usb_device(device_harness):
    device = device_harness.add_device("usb-null", variant="usb", camera="/dev/null", wait=False)
    deadline = time.monotonic() + 60
    while True:
        try:
            with device.api_client(timeout=3) as api:
                if api.get("/v3/paths/list").status_code == 200:
                    return device
        except (httpx.HTTPError, RuntimeError):
            device._ports.clear()
        assert time.monotonic() < deadline, f"USB Device API never answered\n{device.logs()}"
        time.sleep(0.5)


def run_on_init(device):
    with device.api_client() as api:
        response = api.get(f"/v3/config/paths/get/{CAMERA}")
    assert response.status_code == 200, response.text
    return shlex.split(response.json()["runOnInit"])


def option(args, name):
    """The value following ffmpeg option ``name``; options before ``-i`` are input options."""
    return args[args.index(name) + 1]


def test_the_kits_ffmpeg_can_capture_from_v4l2(usb_device):
    devices = usb_device.exec("ffmpeg", "-hide_banner", "-devices").stdout

    assert re.search(r"^\s*D\S*\s+\S*\bv4l2\b", devices, re.MULTILINE), devices


def test_device_api_lists_the_usb_camera_for_the_instance_user(usb_device):
    with usb_device.api_client() as api:
        names = [path["name"] for path in api.get("/v3/paths/list").json()["items"]]

    assert CAMERA in names


def test_the_camera_is_captured_as_mjpg_at_720p15_from_the_mapped_device(usb_device):
    args = run_on_init(usb_device)
    inputs = args[: args.index("-i") + 2]

    assert option(inputs, "-f") == "v4l2"
    assert option(inputs, "-input_format") == "mjpeg"
    assert option(inputs, "-video_size") == "1280x720"
    assert option(inputs, "-framerate") == "15"  # Quality Medium
    assert option(inputs, "-i") == IN_CONTAINER_CAMERA


def test_the_camera_is_encoded_as_1_mbps_h264_with_no_b_frames_and_published_as_usb(usb_device):
    args = run_on_init(usb_device)
    outputs = args[args.index("-i") + 2:]

    assert option(outputs, "-c:v") == "libx264"
    assert option(outputs, "-preset") == "ultrafast"
    assert option(outputs, "-tune") == "zerolatency"
    assert option(outputs, "-bf") == "0"
    assert option(outputs, "-pix_fmt") == "yuv420p"
    assert option(outputs, "-g") == "15"  # a keyframe a second
    assert option(outputs, "-b:v") == "1M"
    assert option(outputs, "-maxrate") == "1M"
    assert option(outputs, "-bufsize") == "500k"
    assert outputs[-1] == f"rtsps://127.0.0.1:8322/{CAMERA}"


def test_the_picture_passes_through_the_filter_env_names(usb_device):
    args = run_on_init(usb_device)
    outputs = args[args.index("-i") + 2:]

    # MediaMTX fills in the variable from the container's environment.
    assert option(outputs, "-vf") == "$USB_CAMERA_FILTER"
    assert usb_device.exec("printenv", "USB_CAMERA_FILTER").stdout.strip() == "null"


@pytest.mark.parametrize("video_filter", ["null", "hflip", "vflip", "hflip,vflip"])
def test_the_kits_ffmpeg_accepts_each_documented_filter(usb_device, video_filter):
    result = usb_device.exec("ffmpeg", "-hide_banner", "-loglevel", "error",
                             "-f", "lavfi", "-i", "testsrc=size=64x48:rate=1", "-frames:v", "1",
                             "-vf", video_filter, "-f", "null", "-", check=False)

    assert result.returncode == 0, result.stderr


def test_device_api_refuses_an_unauthenticated_caller(usb_device):
    with usb_device.api_client(auth=False) as api:
        assert api.get("/v3/paths/list").status_code == 401


def test_device_api_is_not_served_over_plain_http(usb_device):
    plain_url = usb_device.host_api_url.replace("https://", "http://")
    with httpx.Client(auth=usb_device.credentials, timeout=5) as client:
        try:
            response = client.get(f"{plain_url}/v3/paths/list")
        except httpx.HTTPError:
            return
    assert response.status_code == 400


def test_unauthenticated_rtsps_read_is_refused(device_harness, usb_device):
    result = device_harness.probe("-v", "error", "-rtsp_transport", "tcp",
                                  usb_device.rtsps_url(CAMERA, credentials=False))

    assert result.returncode != 0
    assert "401" in result.stderr


def test_plain_rtsp_is_not_served(device_harness, usb_device):
    result = device_harness.probe("-v", "error", "-rtsp_transport", "tcp",
                                  usb_device.rtsps_url(CAMERA, scheme="rtsp", port=8554))

    assert result.returncode != 0
    assert "Connection refused" in result.stderr


def test_device_runs_mediamtx_1_21_2(usb_device):
    with usb_device.api_client() as api:
        assert api.get("/v3/info").json()["version"] == "v1.21.2"


def test_compose_refuses_to_start_without_a_camera(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("INSTANCE_USER=viewer\nINSTANCE_PASS=secret\n")
    env = {key: value for key, value in os.environ.items() if key != "USB_CAMERA"}

    result = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-f", str(KIT_DIR / "usb" / "compose.yaml"),
         "config"],
        capture_output=True, text=True, env=env,
    )

    assert result.returncode != 0
    assert "USB_CAMERA" in result.stderr and "gen-cert.sh" in result.stderr


def test_compose_maps_the_named_camera_into_the_container(tmp_path):
    env_file = tmp_path / ".env"
    by_id = "/dev/v4l/by-id/usb-046d_C922_Pro_Stream_Webcam_2E4A0F8F-video-index0"
    env_file.write_text(f"INSTANCE_USER=viewer\nINSTANCE_PASS=secret\nUSB_CAMERA={by_id}\n")

    result = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-f", str(KIT_DIR / "usb" / "compose.yaml"),
         "config", "--format", "json"],
        capture_output=True, text=True, check=True,
    )

    [device] = json.loads(result.stdout)["services"]["mediamtx"]["devices"]
    assert (device["source"], device["target"]) == (by_id, IN_CONTAINER_CAMERA)


def test_compose_passes_the_filter_from_env_and_defaults_to_no_change(tmp_path):
    env_file = tmp_path / ".env"
    base = "INSTANCE_USER=viewer\nINSTANCE_PASS=secret\nUSB_CAMERA=/dev/null\n"
    env = {key: value for key, value in os.environ.items() if key != "USB_CAMERA_FILTER"}

    filters = []
    for extra in ("", "USB_CAMERA_FILTER=hflip,vflip\n"):
        env_file.write_text(base + extra)
        result = subprocess.run(
            ["docker", "compose", "--env-file", str(env_file), "-f", str(KIT_DIR / "usb" / "compose.yaml"),
             "config", "--format", "json"],
            capture_output=True, text=True, env=env, check=True,
        )
        filters.append(json.loads(result.stdout)["services"]["mediamtx"]["environment"]["USB_CAMERA_FILTER"])

    assert filters == ["null", "hflip,vflip"]
