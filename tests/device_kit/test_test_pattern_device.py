"""The test-pattern Device from the Device Kit, observed from outside as a client would."""

import hashlib
import json
import ssl

import httpx

CAMERA = "test-pattern"
GOP = 30  # the kit encodes with -g 30
FRAMES = 3 * GOP  # packets to read; the decoder emits a few fewer frames


def read_frames(harness, url):
    """ffprobe a Camera from inside the session network: stream info plus the
    picture type of the first FRAMES decoded frames."""
    return harness.probe(
        "-v", "error",
        "-rtsp_transport", "tcp",
        "-select_streams", "v:0",
        "-read_intervals", f"%+#{FRAMES}",
        "-show_entries", "stream=codec_name,width,height,has_b_frames:frame=pict_type",
        "-of", "json",
        url,
    )


def test_authenticated_rtsps_read_receives_h264_with_no_b_frames(device_harness, test_pattern_device):
    result = read_frames(device_harness, test_pattern_device.rtsps_url(CAMERA))

    assert result.returncode == 0, result.stderr
    probe = json.loads(result.stdout)
    [stream] = probe["streams"]
    assert stream["codec_name"] == "h264"
    assert stream["has_b_frames"] == 0
    picture_types = [frame["pict_type"] for frame in probe["frames"]]
    assert len(picture_types) >= 2 * GOP
    assert "B" not in picture_types
    assert "I" in picture_types


def test_device_api_answers_over_https_for_the_instance_user_and_lists_the_camera(test_pattern_device):
    with test_pattern_device.api_client() as api:
        response = api.get("/v3/paths/list")

    assert response.status_code == 200
    assert CAMERA in [path["name"] for path in response.json()["items"]]


def test_unauthenticated_rtsps_read_is_refused(device_harness, test_pattern_device):
    result = read_frames(device_harness, test_pattern_device.rtsps_url(CAMERA, credentials=False))

    assert result.returncode != 0
    assert "401" in result.stderr


def test_wrong_password_rtsps_read_is_refused(device_harness, test_pattern_device):
    url = test_pattern_device.rtsps_url(CAMERA).replace(test_pattern_device.password, "not-the-password")

    result = read_frames(device_harness, url)

    assert result.returncode != 0
    assert "401" in result.stderr


def test_plain_rtsp_read_is_refused_on_the_standard_rtsp_port(device_harness, test_pattern_device):
    result = read_frames(device_harness, test_pattern_device.rtsps_url(CAMERA, scheme="rtsp", port=8554))

    assert result.returncode != 0
    assert "Connection refused" in result.stderr


def test_plain_rtsp_read_is_refused_on_the_rtsps_port(device_harness, test_pattern_device):
    result = read_frames(device_harness, test_pattern_device.rtsps_url(CAMERA, scheme="rtsp"))

    assert result.returncode != 0
    assert '"streams"' not in result.stdout


def test_device_api_refuses_an_unauthenticated_caller(test_pattern_device):
    with test_pattern_device.api_client(auth=False) as api:
        response = api.get("/v3/paths/list")

    assert response.status_code == 401


def test_device_api_is_not_served_over_plain_http(test_pattern_device):
    plain_url = test_pattern_device.host_api_url.replace("https://", "http://")

    with httpx.Client(auth=test_pattern_device.credentials, timeout=5) as client:
        try:
            response = client.get(f"{plain_url}/v3/paths/list")
        except httpx.HTTPError:
            return  # connection-level refusal
    assert response.status_code == 400  # Go's TLS server answers plain HTTP with 400


def test_device_presents_the_certificate_the_kit_generated(test_pattern_device):
    der = ssl.get_server_certificate(("127.0.0.1", test_pattern_device.host_api_port))

    assert hashlib.sha256(ssl.PEM_cert_to_DER_cert(der)).hexdigest() == test_pattern_device.fingerprint


def test_device_runs_mediamtx_1_21_2(test_pattern_device):
    with test_pattern_device.api_client() as api:
        info = api.get("/v3/info").json()

    assert info["version"] == "v1.21.2"
