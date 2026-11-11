"""The CSI Device Kit variant, without a CSI camera.

The compose file is checked as Docker renders it. Where Docker runs arm64
(the variant's image, `1.21.2-rpi`, has no amd64 build), the kit also runs
exactly as a student starts it: MediaMTX, its hardening and the configured
`rpiCamera` source are all real, only libcamera finds no camera, so the
source reports an error and retries while the Device stays up. Playback from a
real CSI module is verified by hand on a Pi 4 and a Pi 5; see
`test_csi_hardware.py`.
"""

import json
import os
import subprocess
import time

import httpx
import pytest

from camera_viewer.quality import LADDERS, Kit, Level, live_setting, patch_for
from harness.devices import KIT_DIR

COMPOSE = KIT_DIR / "csi" / "compose.yaml"
FLIPS = {"CAMERA_HFLIP": "MTX_PATHS_CSI_RPICAMERAHFLIP", "CAMERA_VFLIP": "MTX_PATHS_CSI_RPICAMERAVFLIP"}


def render(tmp_path, env_text="INSTANCE_USER=viewer\nINSTANCE_PASS=secret\n"):
    env_file = tmp_path / ".env"
    env_file.write_text(env_text)
    env = {k: v for k, v in os.environ.items() if k not in ("INSTANCE_USER", "INSTANCE_PASS", *FLIPS)}
    return subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-f", str(COMPOSE), "config", "--format", "json"],
        capture_output=True, text=True, env=env,
    )


def service(tmp_path):
    result = render(tmp_path)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["services"]["mediamtx"]


def test_compose_runs_the_pinned_raspberry_pi_build_of_mediamtx(tmp_path):
    assert service(tmp_path)["image"] == "bluenviron/mediamtx:1.21.2-rpi"


def test_compose_gives_libcamera_what_it_needs_to_reach_the_camera(tmp_path):
    mediamtx = service(tmp_path)

    # The MediaMTX docs' Docker recipe for Raspberry Pi cameras, less
    # `privileged`: the camera's device nodes come from the override that
    # gen-cert.sh writes on the Pi (test_gen_cert.py), never from this file.
    assert not mediamtx.get("privileged")
    assert not mediamtx.get("devices")
    assert "/dev/shm:exec" in mediamtx["tmpfs"]
    udev = [v for v in mediamtx["volumes"] if v["target"] == "/run/udev"]
    assert [(v["source"], v.get("read_only")) for v in udev] == [("/run/udev", True)]


def test_compose_leaves_the_picture_unflipped_by_default(tmp_path):
    environment = service(tmp_path)["environment"]

    assert {environment[mtx] for mtx in FLIPS.values()} == {"false"}


def test_compose_passes_each_flip_from_env_to_mediamtx(tmp_path):
    for flip, mtx in FLIPS.items():
        result = render(tmp_path, f"INSTANCE_USER=viewer\nINSTANCE_PASS=secret\n{flip}=true\n")
        assert result.returncode == 0, result.stderr
        environment = json.loads(result.stdout)["services"]["mediamtx"]["environment"]

        assert {key: environment[key] for key in FLIPS.values()} == {
            key: "true" if key == mtx else "false" for key in FLIPS.values()}


# -- the Device running, with no camera attached -------------------------------

CAMERA = "csi"


def docker_runs_arm64():
    """Asked when the fixture runs, not at import, so collecting the fast tier never calls the daemon."""
    arch = subprocess.run(["docker", "info", "--format", "{{.Architecture}}"],
                          capture_output=True, text=True).stdout.strip()
    return arch in ("aarch64", "arm64")


@pytest.fixture(scope="module")
def csi_device(device_harness):
    if not docker_runs_arm64():
        pytest.skip("the -rpi MediaMTX image is arm/arm64 only")
    return api_answering(device_harness.add_device("csi-none", variant="csi", wait=False))


def api_answering(device):
    """``device`` once its API answers; with no camera, ``add_device`` cannot wait for its Feed."""
    deadline = time.monotonic() + 60
    while True:
        try:
            with device.api_client(timeout=3) as api:
                if api.get("/v3/paths/list").status_code == 200:
                    return device
        except (httpx.HTTPError, RuntimeError):
            device._ports.clear()
        assert time.monotonic() < deadline, f"CSI Device API never answered\n{device.logs()}"
        time.sleep(0.5)


def camera_config(device):
    with device.api_client() as api:
        response = api.get(f"/v3/config/paths/get/{CAMERA}")
    assert response.status_code == 200, response.text
    return response.json()


def test_device_api_lists_the_csi_camera_for_the_instance_user(csi_device):
    with csi_device.api_client() as api:
        names = [path["name"] for path in api.get("/v3/paths/list").json()["items"]]

    assert names == [CAMERA]


def test_the_camera_is_the_pi_camera_booting_at_quality_medium_1080p15_and_1_5_mbps(csi_device):
    config = camera_config(csi_device)

    assert config["source"] == "rpiCamera"
    assert (config["rpiCameraWidth"], config["rpiCameraHeight"]) == (1920, 1080)
    assert config["rpiCameraFPS"] == 15
    assert config["rpiCameraBitrate"] == 1_500_000


def test_the_encoder_is_hardware_where_the_pi_has_one_and_software_on_a_pi_5(csi_device):
    # `auto` is hardware H.264 on a Pi 4 and software H.264 on a Pi 5, which
    # has no encoder; both emit no B-frames, which WebRTC playback requires.
    assert camera_config(csi_device)["rpiCameraCodec"] == "auto"


def test_a_keyframe_every_second_so_a_feed_starts_quickly(csi_device):
    assert camera_config(csi_device)["rpiCameraIDRPeriod"] == 15  # the boot fps


def test_the_camera_is_not_flipped_by_default(csi_device):
    config = camera_config(csi_device)

    assert (config["rpiCameraHFlip"], config["rpiCameraVFlip"]) == (False, False)


def test_flips_set_in_env_reach_the_camera(device_harness):
    if not docker_runs_arm64():
        pytest.skip("the -rpi MediaMTX image is arm/arm64 only")
    device = device_harness.add_device("csi-flip", variant="csi", wait=False,
                                       settings={"CAMERA_HFLIP": "true", "CAMERA_VFLIP": "true"})
    try:
        config = camera_config(api_answering(device))
    finally:
        device_harness.remove_device(device)

    assert (config["rpiCameraHFlip"], config["rpiCameraVFlip"]) == (True, True)


def test_with_no_camera_the_device_stays_up_and_reports_why(csi_device):
    time.sleep(12)  # a few of the source's 5 s retries

    with csi_device.api_client() as api:
        source = api.get(f"/v3/paths/static-sources/get/{CAMERA}").json()
        path = api.get(f"/v3/paths/get/{CAMERA}").json()
    state = csi_device.exec("cat", "/proc/1/cmdline").stdout

    assert source["state"] == "error", source
    assert "camera" in source["lastError"].lower(), source
    assert path["available"] is False
    assert "mediamtx" in state  # PID 1 never died
    logs = csi_device.logs()
    assert logs.count("[RPI Camera source]") >= 2, logs
    assert "panic" not in logs.lower()


def test_device_api_refuses_an_unauthenticated_caller(csi_device):
    with csi_device.api_client(auth=False) as api:
        assert api.get("/v3/paths/list").status_code == 401


def test_device_api_is_not_served_over_plain_http(csi_device):
    plain_url = csi_device.host_api_url.replace("https://", "http://")
    with httpx.Client(auth=csi_device.credentials, timeout=5) as client:
        try:
            response = client.get(f"{plain_url}/v3/paths/list")
        except httpx.HTTPError:
            return
    assert response.status_code == 400


def test_unauthenticated_rtsps_read_is_refused(device_harness, csi_device):
    result = device_harness.probe("-v", "error", "-rtsp_transport", "tcp",
                                  csi_device.rtsps_url(CAMERA, credentials=False))

    assert result.returncode != 0
    assert "401" in result.stderr


def test_plain_rtsp_is_not_served(device_harness, csi_device):
    result = device_harness.probe("-v", "error", "-rtsp_transport", "tcp",
                                  csi_device.rtsps_url(CAMERA, scheme="rtsp", port=8554))

    assert result.returncode != 0
    assert "Connection refused" in result.stderr


def test_device_runs_mediamtx_1_21_2(csi_device):
    with csi_device.api_client() as api:
        assert api.get("/v3/info").json()["version"] == "v1.21.2"


def test_the_kit_device_takes_every_quality_levels_patch_and_reads_back_as_that_level(csi_device):
    """Quality (ADR 0009) PATCHes these five fields through the kit user's ``api`` permission.
    The scripted Device API takes any field name; MediaMTX 1.21.2 refuses unknown ones, so this
    is where the translation's field names meet the real thing (no camera needed)."""
    try:
        for level in Level:
            with csi_device.api_client() as api:
                patched = api.patch(f"/v3/config/paths/patch/{CAMERA}",
                                    json=patch_for(Kit.CSI, level, camera_config(csi_device)))
            assert patched.status_code == 200, patched.text
            assert LADDERS[Kit.CSI].level_of(live_setting(Kit.CSI, camera_config(csi_device))) is level
            assert camera_config(csi_device)["rpiCameraCodec"] == "auto"  # nothing else moved
    finally:
        with csi_device.api_client() as api:
            api.patch(f"/v3/config/paths/patch/{CAMERA}", json=patch_for(Kit.CSI, Level.MEDIUM, {}))
