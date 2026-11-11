"""The Device Kit's GPS Sidecar, observed from outside as the instance would.

Every Device here runs a kit's shipped compose with ``--profile gps`` (the
harness adds only network placement, ``tests/harness/device-gps.yaml``) and
replays a track this module writes, so the expected fixes are known literals.
"""

import hashlib
import itertools
import json
import os
import re
import ssl
import subprocess
import time

import httpx
import pytest

from harness.devices import KIT_DIR
from harness.gps import nmea, write_track

TRACK = "track.nmea"


# A 5 Hz receiver heading 012.5 degrees true at 9.719 knots (5.0 m/s), one
# epoch every 0.2 s: RMC, VTG and GGA, as a u-blox puck sends them.
EPOCHS = [
    ("090000.00", "5130.0000", "35.0"),
    ("090000.20", "5130.0060", "36.0"),
    ("090000.40", "5130.0120", "37.0"),
    ("090000.60", "5130.0180", "38.0"),
    ("090000.80", "5130.0240", "39.0"),
]
TRACK_LINES = [
    line
    for utc, lat, alt in EPOCHS
    for line in (
        nmea(f"GPRMC,{utc},A,{lat},N,00007.5000,W,9.719,12.5,151026,,,A"),
        nmea("GPVTG,12.5,T,,M,9.719,N,18.000,K,A"),
        nmea(f"GPGGA,{utc},{lat},N,00007.5000,W,1,09,0.8,{alt},M,47.0,M,,"),
    )
]


def fix(time_, lat, alt):
    return {"time": time_, "lat": lat, "lon": -0.125, "alt": alt, "speed": pytest.approx(5.0, abs=1e-3),
            "track": 12.5, "mode": "3d", "satellites": 9, "hdop": 0.8}


EXPECTED = [
    fix("2026-10-15T09:00:00.000Z", 51.5, 35.0),
    fix("2026-10-15T09:00:00.200Z", 51.5001, 36.0),
    fix("2026-10-15T09:00:00.400Z", 51.5002, 37.0),
    fix("2026-10-15T09:00:00.600Z", 51.5003, 38.0),
    fix("2026-10-15T09:00:00.800Z", 51.5004, 39.0),
]


def read_fixes(device, count, *, interval, timeout=20):
    """The first ``count`` lines of the stream, each with when it arrived."""
    lines = []
    deadline = time.monotonic() + timeout
    with device.gps_client() as client, client.stream("GET", "/v1/fixes", params={"interval": interval}) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"] == "application/x-ndjson"
        for line in response.iter_lines():
            lines.append((time.monotonic(), json.loads(line)))
            if len(lines) == count or time.monotonic() > deadline:
                break
    return lines


@pytest.fixture(scope="module")
def replaying_device(device_harness, tmp_path_factory):
    tracks = write_track(tmp_path_factory.mktemp("tracks"), TRACK_LINES)
    device = device_harness.add_device(
        "gps-replay", gps=True, gps_tracks=tracks, settings={"GPS_SOURCE": f"replay:{TRACK}"})
    yield device
    device_harness.remove_device(device)


def test_a_replayed_track_streams_each_of_its_fixes_as_one_json_line(replaying_device):
    fixes = [fix for _, fix in read_fixes(replaying_device, 12, interval=0.2)]

    for fix in fixes:
        assert fix in EXPECTED
    assert {fix["time"] for fix in fixes} == {fix["time"] for fix in EXPECTED}


def test_the_stream_sends_at_most_one_fix_per_requested_interval(replaying_device):
    # The track runs at 5 Hz, so a 1 s interval must skip epochs rather than queue them.
    lines = read_fixes(replaying_device, 5, interval=1)

    gaps = [later - earlier for (earlier, _), (later, _) in itertools.pairwise(lines)]
    assert all(0.95 <= gap <= 1.5 for gap in gaps), gaps
    assert all(fix in EXPECTED for _, fix in lines)


def test_the_sidecar_presents_the_certificate_the_kit_generated(replaying_device):
    pem = ssl.get_server_certificate(("127.0.0.1", replaying_device.host_gps_port))

    assert hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest() == replaying_device.fingerprint


@pytest.mark.parametrize("credentials", [None, ("viewer", "not-the-password"), ("someone", "else")],
                         ids=["none", "wrong-password", "wrong-user"])
def test_the_stream_refuses_missing_or_wrong_credentials(replaying_device, credentials):
    with replaying_device.gps_client(auth=False) as client:
        response = client.get("/v1/fixes", params={"interval": 1}, auth=credentials)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")
    assert "lat" not in response.text


def test_the_sidecar_is_not_served_over_plain_http(replaying_device):
    plain_url = replaying_device.host_gps_url.replace("https://", "http://")

    with httpx.Client(auth=replaying_device.credentials, timeout=5) as client:
        try:
            response = client.get(f"{plain_url}/v1/fixes", params={"interval": 1})
        except httpx.HTTPError:
            return  # a TLS server drops a plain-HTTP client
    assert response.status_code == 400


@pytest.mark.parametrize("interval", ["0", "-1", "abc", "nan", "86400"])
def test_an_interval_out_of_range_is_refused(replaying_device, interval):
    with replaying_device.gps_client() as client:
        response = client.get("/v1/fixes", params={"interval": interval})

    assert response.status_code == 400


def test_any_other_path_is_not_found(replaying_device):
    with replaying_device.gps_client() as client:
        assert client.get("/v3/paths/list").status_code == 404


# Each would put the Device at 10 degrees north if it got through.
ELSEWHERE = "GPRMC,{utc},A,1000.0000,N,00007.5000,W,9.719,12.5,151026,,,A"


def bad_checksum(body):
    good = nmea(body)
    return good[:-2] + f"{int(good[-2:], 16) ^ 0x01:02X}"


HOSTILE_LINES = [
    bad_checksum(ELSEWHERE.format(utc="090000.10")),
    "$" + ELSEWHERE.format(utc="090000.30"),  # no checksum at all
    nmea(ELSEWHERE.format(utc="090000.50") + "," + "x" * 1100),  # a correct checksum, but over 1 KB
    "\x00\xff garbage \x1b[2J",
    nmea(ELSEWHERE.format(utc="090000.70")).lower(),  # not NMEA's alphabet
]


@pytest.fixture(scope="module")
def hostile_track_device(device_harness, tmp_path_factory):
    good = iter(TRACK_LINES)
    lines = [line for hostile in HOSTILE_LINES for line in (next(good), next(good), next(good), hostile)]
    tracks = write_track(tmp_path_factory.mktemp("tracks"), lines + list(good))
    device = device_harness.add_device(
        "gps-hostile", gps=True, gps_tracks=tracks, settings={"GPS_SOURCE": f"replay:{TRACK}"})
    yield device
    device_harness.remove_device(device)


def test_the_shipped_sample_track_replays_as_a_moving_3d_fix(device_harness, unique_label):
    device = device_harness.add_device(unique_label("gps-sample"), gps=True,
                                       settings={"GPS_SOURCE": "replay:sample.nmea"})

    fixes = [fix for _, fix in read_fixes(device, 3, interval=1)]

    assert all(fix["mode"] == "3d" and fix["satellites"] == 9 for fix in fixes), fixes
    assert all(abs(fix["lat"] - 38.8895) < 0.01 and abs(fix["lon"] + 77.0353) < 0.01 for fix in fixes), fixes
    assert len({(fix["lat"], fix["lon"]) for fix in fixes}) == 3
    assert all(fix["speed"] == pytest.approx(8.0, abs=0.01) for fix in fixes)


# -- the shipped compose, rendered as `docker compose` reads it (no daemon needed) --

KIT_ENV = {"INSTANCE_USER": "viewer", "INSTANCE_PASS": "secret", "USB_CAMERA": "/dev/null"}


def rendered(variant, *profile, **settings):
    result = subprocess.run(
        ["docker", "compose", *profile, "config", "--format", "json"],
        cwd=KIT_DIR / variant, capture_output=True, text=True, check=True,
        env={**os.environ, **KIT_ENV, **settings},
    )
    return json.loads(result.stdout)["services"]


@pytest.mark.parametrize("variant", ["csi", "usb", "test-pattern"])
def test_without_the_gps_profile_a_kit_runs_only_mediamtx(variant):
    assert list(rendered(variant)) == ["mediamtx"]


@pytest.mark.parametrize("variant", ["csi", "usb", "test-pattern"])
def test_the_gps_profile_adds_the_shared_sidecar_on_the_stock_python_image(variant):
    gps = rendered(variant, "--profile", "gps", GPS_TTY="/dev/serial/by-id/usb-u-blox-if00")["gps"]

    assert gps["image"] == "python:3.13-slim" and "build" not in gps
    assert gps["restart"] == "unless-stopped"
    assert [(port["target"], port["published"]) for port in gps["ports"]] == [(9998, "9998")]
    assert [(device["source"], device["target"]) for device in gps["devices"]] == [
        ("/dev/serial/by-id/usb-u-blox-if00", "/dev/gps")]
    mounts = {volume["target"]: volume["source"] for volume in gps["volumes"]}
    assert mounts == {"/gps": str(KIT_DIR / "gps"), "/certs": str(KIT_DIR / variant / "certs")}
    assert gps["environment"]["INSTANCE_USER"] == "viewer"


def wait_exited(device, timeout=30):
    deadline = time.monotonic() + timeout
    while (state := device.gps_state())["State"] != "exited":
        assert time.monotonic() < deadline, state
        time.sleep(0.3)
    return state


@pytest.mark.parametrize(("settings", "message"), [
    ({}, "GPS_TTY is not set"),
    ({"GPS_TTY": "/dev/null"}, "GPS_TTY /dev/null is not a serial device"),
], ids=["unset", "not-a-tty"])
def test_with_no_usable_receiver_the_sidecar_exits_saying_what_to_fix(device_harness, unique_label, settings,
                                                                       message):
    device = device_harness.add_device(unique_label("gps-tty"), gps=True, settings=settings, wait=False)
    device.wait_ready()

    state = wait_exited(device)

    assert state["ExitCode"] == 1
    logs = device.gps_logs()
    assert message in logs and "gen-cert.sh" in logs and "replay:" in logs
    assert "Traceback" not in logs
    with device.api_client() as api:  # the Camera carries on without it
        assert api.get("/v3/paths/list").status_code == 200


def test_a_receiver_path_that_does_not_exist_stops_compose_up_naming_it(device_harness, unique_label):
    missing = "/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_7_-_GPS_GNSS_Receiver-if00"

    with pytest.raises(RuntimeError, match=re.escape(missing)):
        device_harness.add_device(unique_label("gps-gone"), gps=True, settings={"GPS_TTY": missing})


def test_bad_checksums_overlong_and_garbage_lines_are_skipped(hostile_track_device):
    fixes = [fix for _, fix in read_fixes(hostile_track_device, 12, interval=0.2)]

    assert all(fix in EXPECTED for fix in fixes), fixes
    assert {fix["time"] for fix in fixes} == {fix["time"] for fix in EXPECTED}
