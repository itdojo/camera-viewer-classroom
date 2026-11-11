"""The scripted Device API: a fake Device whose config a test can PATCH and read back.

Quality (#47) writes a Camera's level through ``/v3/config/paths/patch/{name}``
and tells a Device restart from a foreign change by ``/v3/info`` ``started``.
These tests pin the fake to what a real kit Device on MediaMTX 1.21.2 answers
(checked against a test-pattern Device, #48), so a test can drive both
without a camera.

Its scripted GPS Sidecar (#57) serves 9998 as the kit's does, but streams whatever lines a test
chose, byte for byte, and records each request and connection, so a test can prove what the
instance refuses and that an impostor never receives the credentials.
"""

import base64
import time
from datetime import datetime

import httpx
import pytest

# The fake runs on Python 3; this is the instance image's base, already pulled in CI.
IMAGE = "python:3.13-slim"

CSI_CONFIG = {
    "name": "csi", "source": "rpiCamera", "rpiCameraWidth": 1920, "rpiCameraHeight": 1080,
    "rpiCameraFPS": 30, "rpiCameraBitrate": 4000000, "rpiCameraIDRPeriod": 30, "rpiCameraHFlip": True,
}


def parse_started(text):
    return datetime.fromisoformat(text)  # truncates MediaMTX's nanoseconds to microseconds


def run_on_init_once(api, wanted: str, seconds: float = 10) -> str:
    """The test pattern's ``runOnInit`` once it holds ``wanted``, or as it stands at the deadline.
    MediaMTX 1.21.2 answers a config PATCH before it stores the new config (its core replies,
    then closes the path and its ffmpeg, then stores), so a GET straight after the 200 can read
    the old one; on a slow CI runner it did."""
    deadline = time.monotonic() + seconds
    while True:
        served = api.get("/v3/config/paths/get/test-pattern").json()["runOnInit"]
        if wanted in served or time.monotonic() > deadline:
            return served
        time.sleep(0.1)


@pytest.fixture
def scripted(device_harness, unique_label):
    return device_harness.add_scripted_device(
        unique_label("fake"), IMAGE, paths=[{"name": "csi", "available": True}],
        path_configs={"csi": CSI_CONFIG}, started="2026-10-15T08:00:00.000000001Z")


def test_a_patch_is_recorded_and_served_back_merged_into_the_path_config(scripted):
    with scripted.api_client() as api:
        response = api.patch("/v3/config/paths/patch/csi", json={"rpiCameraFPS": 15, "rpiCameraIDRPeriod": 15})
        config = api.get("/v3/config/paths/get/csi").json()

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert scripted.patches() == [{"path": "csi", "body": {"rpiCameraFPS": 15, "rpiCameraIDRPeriod": 15}}]
    assert config == {**CSI_CONFIG, "rpiCameraFPS": 15, "rpiCameraIDRPeriod": 15}


def test_a_path_with_no_config_answers_404_as_mediamtx_does(scripted):
    with scripted.api_client() as api:
        patched = api.patch("/v3/config/paths/patch/nope", json={"rpiCameraFPS": 15})
        fetched = api.get("/v3/config/paths/get/nope")

    assert (patched.status_code, patched.json()) == (404, {"status": "error", "error": "path not found"})
    assert fetched.status_code == 404
    assert scripted.patches() == [{"path": "nope", "body": {"rpiCameraFPS": 15}}]  # received, though refused


def test_a_scripted_restart_changes_started_and_forgets_patches_as_a_real_device_does(scripted):
    with scripted.api_client() as api:
        api.patch("/v3/config/paths/patch/csi", json={"rpiCameraFPS": 15})
        first = api.get("/v3/info").json()
        scripted.restart(started="2026-10-15T09:30:00.000000002Z")
        second = api.get("/v3/info").json()
        config = api.get("/v3/config/paths/get/csi").json()

    assert first == {"version": "v1.21.2", "started": "2026-10-15T08:00:00.000000001Z"}
    assert second == {"version": "v1.21.2", "started": "2026-10-15T09:30:00.000000002Z"}
    assert config == CSI_CONFIG  # API changes live only in the Device's memory


def test_a_scripted_failure_answers_every_patch_until_cleared(scripted):
    with scripted.api_client() as api:
        scripted.fail_patches(403, {"status": "error", "error": "insufficient permissions"})
        refused = api.patch("/v3/config/paths/patch/csi", json={"rpiCameraFPS": 15})
        unchanged = api.get("/v3/config/paths/get/csi").json()
        scripted.fail_patches(None)
        accepted = api.patch("/v3/config/paths/patch/csi", json={"rpiCameraFPS": 15})

    assert (refused.status_code, refused.json()["error"]) == (403, "insufficient permissions")
    assert unchanged["rpiCameraFPS"] == 30
    assert accepted.status_code == 200
    assert scripted.patches() == [{"path": "csi", "body": {"rpiCameraFPS": 15}}] * 2  # every one received


def test_a_real_kit_device_answers_as_the_fake_does_to_its_instance_user(device_harness, unique_label):
    """The fake's fidelity, and the kit's grant: ``read`` plus ``api`` lets the
    instance user PATCH a path (ADR 0009 asked whether it would)."""
    device = device_harness.add_device(unique_label("real"))
    with device.api_client() as api:
        started = api.get("/v3/info").json()["started"]
        command = api.get("/v3/config/paths/get/test-pattern").json()["runOnInit"]
        patched = api.patch("/v3/config/paths/patch/test-pattern",
                            json={"runOnInit": command.replace("-b:v 2M", "-b:v 1M")})
        served = run_on_init_once(api, "-b:v 1M")
        missing = api.patch("/v3/config/paths/patch/nope", json={"runOnInit": command})
        unchanged = api.get("/v3/info").json()["started"]

    assert (patched.status_code, patched.json()) == (200, {"status": "ok"})
    assert "-b:v 1M" in served and "-b:v 2M" not in served
    assert (missing.status_code, missing.json()) == (404, {"status": "error", "error": "path not found"})
    assert unchanged == started

    device.restart()
    with device.api_client() as api:
        # Go's RFC3339Nano drops trailing zeros, so compare times, never the text.
        assert parse_started(api.get("/v3/info").json()["started"]) > parse_started(started)
        assert api.get("/v3/config/paths/get/test-pattern").json()["runOnInit"] == command


# -- the scripted GPS Sidecar ---------------------------------------------------------------------

SIDECAR_LINES = [b'{"lat": 91}', b"not json", b"\xff\xfe\x00 binary", b"x" * 4000, b""]


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def raw_lines(response, count):
    """The first ``count`` newline-ended lines of a streamed body, as bytes."""
    buffer = b""
    for chunk in response.iter_raw():
        buffer += chunk
        if buffer.count(b"\n") >= count:
            break
    return buffer.split(b"\n")[:count]


def test_the_scripted_sidecar_streams_its_lines_over_and_over_and_records_each_request(
        device_harness, unique_label):
    fake = device_harness.add_scripted_device(unique_label("fake-gps"), IMAGE, paths=[],
                                              sidecar_lines=SIDECAR_LINES)

    with fake.gps_client() as gps, gps.stream("GET", "/v1/fixes?interval=1") as response:
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/x-ndjson"
        assert response.headers["transfer-encoding"] == "chunked"  # as the kit's Sidecar streams
        seen = raw_lines(response, 2 * len(SIDECAR_LINES))

    assert seen == SIDECAR_LINES * 2  # byte for byte, the oversized and binary ones included
    assert fake.sidecar_requests() == [{"path": "/v1/fixes?interval=1", "authorization": basic(*fake.credentials)}]
    assert fake.sidecar_connections() == 1


def test_an_impostor_sidecar_presents_another_certificate_and_never_reads_a_request(
        device_harness, unique_label):
    impostor = device_harness.add_scripted_device(unique_label("impostor"), IMAGE, paths=[],
                                                  sidecar_lines=SIDECAR_LINES, sidecar_impostor=True)

    with impostor.api_client() as api:  # the Device API still presents the Device's own certificate
        assert api.get("/v3/info").status_code == 200
    with pytest.raises(httpx.ConnectError), impostor.gps_client() as gps:  # trusts only the Device's
        gps.get("/v1/fixes?interval=1")

    assert impostor.sidecar_fingerprint != impostor.fingerprint
    assert impostor.sidecar_requests() == []
    assert impostor.sidecar_connections() == 1  # the refused handshake still counts
