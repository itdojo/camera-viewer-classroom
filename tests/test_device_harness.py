"""The Device harness itself: what later Seam A tests rely on."""

import json
import ssl
import subprocess

import httpx
import pytest

from harness import DeviceHarness
from harness.devices import SESSION_LABEL

# Drives Docker directly rather than through a harness fixture (see tests/conftest.py).
pytestmark = pytest.mark.docker


def docker_lines(*args):
    out = subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def session_containers(session_id):
    return docker_lines("ps", "-a", "--filter", f"label={SESSION_LABEL}={session_id}", "--format", "{{.Names}}")


def session_networks(session_id):
    return docker_lines("network", "ls", "--filter", f"label={SESSION_LABEL}={session_id}", "--format", "{{.Name}}")


def lists_camera(device):
    with device.api_client() as api:
        return device.cameras[0] in [p["name"] for p in api.get("/v3/paths/list").json()["items"]]


def test_a_session_runs_several_devices_and_tears_all_of_them_down(tmp_path):
    with DeviceHarness(tmp_path) as harness:
        first = harness.add_device("cam-a")
        second = harness.add_device("cam-b")

        assert lists_camera(first) and lists_camera(second)
        assert first.host_api_port != second.host_api_port
        assert first.fingerprint != second.fingerprint
        assert len(session_containers(harness.session_id)) == 2

    assert session_containers(harness.session_id) == []
    assert session_networks(harness.session_id) == []


def test_another_container_reaches_each_device_by_label_on_the_session_network(tmp_path):
    with DeviceHarness(tmp_path) as harness:
        devices = [harness.add_device("cam-a"), harness.add_device("cam-b")]

        for device in devices:
            result = harness.probe("-v", "error", "-show_entries", "stream=codec_name", "-of", "json",
                                   device.rtsps_url())
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout)["streams"] == [{"codec_name": "h264"}]


def test_two_sessions_on_one_docker_host_do_not_collide(tmp_path):
    with DeviceHarness(tmp_path / "one") as one, DeviceHarness(tmp_path / "two") as two:
        a = one.add_device("cam")
        b = two.add_device("cam")  # same label, different session

        assert one.network != two.network
        assert a.host_api_port != b.host_api_port
        assert lists_camera(a) and lists_camera(b)

    assert session_containers(one.session_id) == session_containers(two.session_id) == []


def test_removing_a_device_mid_session_removes_only_that_device(tmp_path):
    with DeviceHarness(tmp_path) as harness:
        keep = harness.add_device("keep")
        gone = harness.add_device("gone")

        harness.remove_device(gone)

        assert session_containers(harness.session_id) == [f"{keep.project}-mediamtx-1"]
        assert lists_camera(keep)


def test_a_stopped_device_is_unreachable_until_started_again(tmp_path):
    with DeviceHarness(tmp_path) as harness:
        device = harness.add_device("cam")
        old_port = device.host_api_port

        device.stop()
        with pytest.raises(httpx.HTTPError):
            trust_device = ssl.create_default_context(cafile=str(device.cert_path))
            with httpx.Client(verify=trust_device, timeout=2) as client:
                client.get(f"https://127.0.0.1:{old_port}/v3/paths/list")

        device.start()
        assert lists_camera(device)


def test_a_paused_device_stops_answering_until_unpaused(tmp_path):
    with DeviceHarness(tmp_path) as harness:
        device = harness.add_device("cam")

        device.pause()
        with pytest.raises(httpx.TimeoutException):
            with device.api_client(timeout=2) as api:
                api.get("/v3/paths/list")

        device.unpause()
        device.wait_ready()
        assert lists_camera(device)


def test_a_rebuilt_device_presents_a_new_certificate(tmp_path):
    with DeviceHarness(tmp_path) as harness:
        device = harness.add_device("cam")
        before = device.fingerprint

        after = device.regenerate_cert()

        assert after != before
        assert lists_camera(device)  # the client trusts the new certificate file
