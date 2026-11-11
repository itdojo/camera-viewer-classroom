"""Seam A: a Camera's Quality, set at its Device through the Device API (spec #46, ADR 0009; #47).

CSI translation runs against the scripted Device API, which records every PATCH and serves
the config back, so no camera module is needed. The end-to-end level change, return to Live and
re-apply after a restart run on a real kit Device: the test-pattern kit, on the USB ladder.

Every test here runs on one instance of this module's own, reading each Device's start time every
second (``CV_QUALITY_CHECK_SECONDS``) and its Cameras' configs every 5 s
(``CV_QUALITY_CONFIG_CHECK_SECONDS``), so a re-apply after a Device restart shows within a couple
of seconds rather than ten, and a change by another instance within 5 s rather than 60.
"""

import time

import pytest

CSI_MEDIUM = {
    "name": "csi", "source": "rpiCamera", "rpiCameraCamID": 0, "rpiCameraWidth": 1920, "rpiCameraHeight": 1080,
    "rpiCameraFPS": 15, "rpiCameraCodec": "auto", "rpiCameraBitrate": 1500000, "rpiCameraIDRPeriod": 15,
    "rpiCameraHFlip": True, "rpiCameraVFlip": False,
}
CSI_LOW = {"rpiCameraWidth": 1280, "rpiCameraHeight": 720, "rpiCameraFPS": 15, "rpiCameraBitrate": 800000,
           "rpiCameraIDRPeriod": 15}
CSI_HIGH = {"rpiCameraWidth": 1920, "rpiCameraHeight": 1080, "rpiCameraFPS": 30, "rpiCameraBitrate": 4000000,
            "rpiCameraIDRPeriod": 30}
CHECK_SECONDS = 1
CONFIG_CHECK_SECONDS = 5


@pytest.fixture(scope="module")
def own(instances, device_harness):
    """This module's instance. ``device_harness`` is named so the instance starts before
    ``no_leaked_devices`` takes its snapshot."""
    instance = instances.start("quality", env={"CV_QUALITY_CHECK_SECONDS": str(CHECK_SECONDS),
                                               "CV_QUALITY_CONFIG_CHECK_SECONDS": str(CONFIG_CHECK_SECONDS)})
    yield instance
    instances.remove(instance)


@pytest.fixture
def scripted_csi(own, instance_image, device_harness, unique_label):
    """A fake CSI Device at Medium, added to ``own``; yields it and its Camera id."""
    label = unique_label("csi")
    device = device_harness.add_scripted_device(
        label, instance_image, paths=[{"name": "csi", "available": True}], path_configs={"csi": CSI_MEDIUM},
        started="2026-10-15T08:00:00.000000001Z")
    own.add_device(device, label=label)
    yield device, f"{label}/csi"
    with own.client() as api:
        api.delete(f"/api/devices/{label}")


def quality_of(api, camera_id):
    return next(c for c in api.get("/api/cameras").json() if c["id"] == camera_id)["quality"]


def set_quality(api, camera_id, mode, **extra):
    return api.put(f"/api/cameras/{camera_id}/quality", json={"mode": mode, **extra})


def eventually(check, timeout=15.0, interval=0.3):
    """Poll ``check()`` until it returns something truthy; return that, or fail with the last value."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value or time.monotonic() > deadline:
            assert value, f"still {value!r} after {timeout} s"
            return value
        time.sleep(interval)


def device_config(device, camera):
    with device.api_client() as api:
        return api.get(f"/v3/config/paths/get/{camera}").json()


def ours(device):
    """The PATCHes this instance sent: every one carries a whole level."""
    return [p["body"] for p in device.patches() if "rpiCameraIDRPeriod" in p["body"]]


# -- CSI translation (scripted Device API) ---------------------------------------------------------


def test_a_csi_camera_starts_on_auto_at_the_level_its_device_reports(own, scripted_csi):
    """Read when the Device is added, so the level shows at once, not at the next check."""
    device, camera = scripted_csi

    with own.client() as api:
        quality = quality_of(api, camera)

    assert quality == {"mode": "auto", "level": "medium", "available": True, "reason": None,
                       "changed_elsewhere": False, "error": None}
    assert device.patches() == []  # reading the level writes nothing


def test_picking_a_level_on_a_csi_camera_sets_five_rpicamera_fields_and_nothing_else(own, scripted_csi):
    device, camera = scripted_csi

    with own.client() as api:
        response = set_quality(api, camera, "low")

    assert response.status_code == 200, response.text
    assert response.json()["quality"]["mode"] == "low"
    assert response.json()["quality"]["level"] == "low"
    assert device.patches() == [{"path": "csi", "body": CSI_LOW}]
    assert device_config(device, "csi") == {**CSI_MEDIUM, **CSI_LOW}  # flips, codec, camera untouched


def test_choosing_auto_keeps_the_level_it_is_at(own, scripted_csi):
    """Auto moves the level only from a pulled Feed's loss; this Camera has no media to pull."""
    device, camera = scripted_csi

    with own.client() as api:
        assert set_quality(api, camera, "high").status_code == 200
        response = set_quality(api, camera, "auto")

    assert response.status_code == 200, response.text
    assert (response.json()["quality"]["mode"], response.json()["quality"]["level"]) == ("auto", "high")
    assert ours(device) == [CSI_HIGH]


def test_a_device_restart_gets_its_level_back_quietly(own, scripted_csi):
    device, camera = scripted_csi
    with own.client() as api:
        assert set_quality(api, camera, "low").status_code == 200

        device.restart()  # back to its boot config, Medium, with a new `started`

        eventually(lambda: len(ours(device)) == 2)
        assert ours(device) == [CSI_LOW, CSI_LOW]
        assert device_config(device, "csi") == {**CSI_MEDIUM, **CSI_LOW}
        quality = quality_of(api, camera)
    assert (quality["mode"], quality["level"], quality["changed_elsewhere"]) == ("low", "low", False)


def test_checks_between_config_reads_read_only_when_the_device_started(own, scripted_csi):
    """Polling cost: a Device that keeps running costs one ``/v3/info`` a check and one config read
    per Camera every config interval; a restart still shows within a check interval."""
    device, _ = scripted_csi
    assert device.config_reads() == ["csi"]  # at add

    time.sleep(3 * CHECK_SECONDS)
    assert device.config_reads() == ["csi"]  # the checks since read only /v3/info

    eventually(lambda: len(device.config_reads()) == 2, timeout=CONFIG_CHECK_SECONDS + 2 * CHECK_SECONDS)
    device.restart()  # well before the next config read is due
    eventually(lambda: len(device.config_reads()) == 3, timeout=3 * CHECK_SECONDS)


def test_a_change_from_another_client_is_flagged_and_stops_re_applying(own, scripted_csi):
    """On a pinned Camera with no write pending, while its Device keeps running: the periodic
    config read finds it within its interval."""
    device, camera = scripted_csi
    with own.client() as api:
        assert set_quality(api, camera, "low").status_code == 200

        with device.api_client() as other:  # a second instance, say
            other.patch("/v3/config/paths/patch/csi", json={"rpiCameraFPS": 5}).raise_for_status()

        flagged = eventually(lambda: (q := quality_of(api, camera))["changed_elsewhere"] and q,
                             timeout=CONFIG_CHECK_SECONDS + 3 * CHECK_SECONDS)
        assert flagged["mode"] == "low"
        assert flagged["level"] is None  # 720p at 5 fps and 800 kbit/s is no level

        device.restart()
        time.sleep(4 * CHECK_SECONDS)
        assert ours(device) == [CSI_LOW]  # a restart does not bring the re-apply back
        assert quality_of(api, camera)["changed_elsewhere"] is True

        picked = set_quality(api, camera, "high")
        assert picked.status_code == 200, picked.text
        assert picked.json()["quality"]["changed_elsewhere"] is False
        time.sleep(3 * CHECK_SECONDS)
        assert quality_of(api, camera)["changed_elsewhere"] is False


def test_choosing_auto_also_takes_control_back(own, scripted_csi):
    device, camera = scripted_csi
    with own.client() as api:
        assert set_quality(api, camera, "low").status_code == 200
        with device.api_client() as other:
            other.patch("/v3/config/paths/patch/csi", json={"rpiCameraWidth": 1920, "rpiCameraHeight": 1080})
        eventually(lambda: quality_of(api, camera)["changed_elsewhere"])

        response = set_quality(api, camera, "auto")

        assert response.status_code == 200, response.text
        assert (response.json()["quality"]["mode"], response.json()["quality"]["level"]) == ("auto", None)
        time.sleep(3 * CHECK_SECONDS)
        assert quality_of(api, camera)["changed_elsewhere"] is False
        assert ours(device) == [CSI_LOW]


@pytest.mark.parametrize(("status", "body", "answer", "code"), [
    (401, {"status": "error", "error": "authentication error"}, 422, "device_credentials_rejected"),
    (403, {"status": "error", "error": "insufficient permissions"}, 422, "device_forbidden"),
    (400, {"status": "error", "error": "json: unknown field \"rpiCameraIDRPeriod\""}, 502, "device_rejected"),
])
def test_a_device_that_refuses_the_change_says_why_and_nothing_changes(own, scripted_csi, status, body,
                                                                       answer, code):
    device, camera = scripted_csi
    device.fail_patches(status, body)

    with own.client() as api:
        response = set_quality(api, camera, "low")
        quality = quality_of(api, camera)

    assert (response.status_code, response.json()["error"]) == (answer, code), response.text
    assert quality["error"]["code"] == code
    assert quality["mode"] == "auto"
    if status == 400:
        assert "unknown field" in quality["error"]["message"]  # the Device's own words


def test_a_level_change_that_splits_a_recording_needs_a_confirm(own, scripted_csi):
    device, camera = scripted_csi
    with own.client() as api:
        eventually(lambda: quality_of(api, camera)["level"] == "medium")
        recording = api.post("/api/recordings", json={"camera": camera})
        assert recording.status_code == 201, recording.text
        try:
            refused = set_quality(api, camera, "low")  # 1080p to 720p drops viewers
            within = set_quality(api, camera, "high")  # same resolution: viewers keep watching
            confirmed = set_quality(api, camera, "minimum", confirm=True)
        finally:
            api.post(f"/api/recordings/{recording.json()['id']}/stop")

    assert (refused.status_code, refused.json()["error"]) == (409, "confirm_required"), refused.text
    assert "splits the Recording" in refused.json()["message"]
    assert within.status_code == 200, within.text
    assert confirmed.status_code == 200, confirmed.text
    assert [body["rpiCameraFPS"] for body in ours(device)] == [30, 5]


def test_a_path_that_is_no_kit_camera_has_no_quality(own, instance_image, device_harness, unique_label):
    label = unique_label("relay")
    device = device_harness.add_scripted_device(
        label, instance_image, paths=[{"name": "cam", "available": True}],
        path_configs={"cam": {"name": "cam", "source": "rtsp://10.0.0.9:554/stream"}})
    own.add_device(device, label=label)
    camera = f"{label}/cam"

    with own.client() as api:
        quality = eventually(lambda: (q := quality_of(api, camera))["reason"] and q)
        refused = set_quality(api, camera, "low")
        api.delete(f"/api/devices/{label}")

    assert quality["available"] is False
    assert "not a Device Kit camera" in quality["reason"]
    assert (refused.status_code, refused.json()["reason"]) == (409, "unsupported")
    assert device.patches() == []


def test_an_rtsp_url_camera_has_its_quality_menu_disabled(own, unique_label):
    label = unique_label("manual")
    with own.client() as api:
        added = api.post("/api/cameras", json={"device": label, "url": "rtsp://10.0.0.9:8554/cam"})
        assert added.status_code == 201, added.text
        quality = quality_of(api, f"{label}/cam")
        refused = set_quality(api, f"{label}/cam", "low")
        api.delete(f"/api/devices/{label}")

    assert quality["available"] is False
    assert "RTSP URL" in quality["reason"]
    assert (refused.status_code, refused.json()["error"], refused.json()["reason"]) == \
        (409, "quality_unavailable", "no_device_api")


def test_an_unknown_level_is_refused(own, scripted_csi):
    _, camera = scripted_csi
    with own.client() as api:
        assert set_quality(api, camera, "ultra").status_code == 422
        assert api.put("/api/cameras/nope/none/quality", json={"mode": "low"}).status_code == 404


def test_each_cameras_mode_and_level_survive_an_instance_restart(own, scripted_csi):
    device, camera = scripted_csi
    with own.client() as api:
        assert set_quality(api, camera, "minimum").status_code == 200

    own.restart()

    with own.client() as api:
        quality = quality_of(api, camera)
        time.sleep(3 * CHECK_SECONDS)
        after_checks = quality_of(api, camera)
    assert (quality["mode"], quality["level"]) == ("minimum", "minimum")
    assert after_checks == quality
    assert len(ours(device)) == 1  # nothing to put back: the Device never restarted


# -- a real kit Device (test-pattern, USB ladder) -----------------------------------------------------


@pytest.fixture
def kit_device(own, device_harness, unique_label):
    device = device_harness.add_device(unique_label("tp"))
    own.add_device(device)
    yield device, f"{device.label}/test-pattern"
    with own.client() as api:
        api.delete(f"/api/devices/{device.label}")
    device_harness.remove_device(device)


def live_at(api, camera, width, height):
    view = next(c for c in api.get("/api/cameras").json() if c["id"] == camera)
    return view["feed_status"] == "live" and view["resolution"] == {"width": width, "height": height}


def test_a_level_change_on_a_kit_device_returns_the_feed_to_live_at_the_new_resolution(own, kit_device):
    device, camera = kit_device
    with own.client(timeout=30) as api:
        # A Recording holds the pull, so the Feed is Live before and after; the change drops
        # viewers (every ffmpeg step does), so it splits the Recording and needs the confirm.
        recording = api.post("/api/recordings", json={"camera": camera})
        assert recording.status_code == 201, recording.text
        try:
            eventually(lambda: live_at(api, camera, 1280, 720), timeout=30)
            assert set_quality(api, camera, "low").json()["error"] == "confirm_required"

            response = set_quality(api, camera, "low", confirm=True)

            assert response.status_code == 200, response.text
            eventually(lambda: live_at(api, camera, 640, 360), timeout=40)
        finally:
            api.post(f"/api/recordings/{recording.json()['id']}/stop")

    command = device_config(device, "test-pattern")["runOnInit"]
    assert "testsrc=size=640x360:rate=15 " in command
    assert "-g 15 -b:v 500k -maxrate 500k -bufsize 250k " in command


def test_a_step_that_drops_viewers_never_reads_degraded_or_down(own, kit_device):
    """Spec story 18: a deliberate step is no fault. On the ffmpeg kit every step restarts
    ffmpeg, so the Relay's pull drops for a few seconds; the Feed keeps the Feed Status it had
    until the source is back, then reads from the new pull. Polled rather than followed, so a
    one-sample Degraded can't slip between two events."""
    _, camera = kit_device
    seen, sizes = [], []
    with own.client(timeout=30) as api:
        recording = api.post("/api/recordings", json={"camera": camera})
        assert recording.status_code == 201, recording.text
        try:
            eventually(lambda: live_at(api, camera, 1280, 720), timeout=30)
            time.sleep(2)  # past the pull's first seconds
            assert set_quality(api, camera, "low", confirm=True).status_code == 200
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                view = next(c for c in api.get("/api/cameras").json() if c["id"] == camera)
                if seen[-1:] != [view["feed_status"]]:
                    seen.append(view["feed_status"])
                sizes.append(view["resolution"]["width"])
                time.sleep(0.25)
        finally:
            api.post(f"/api/recordings/{recording.json()['id']}/stop")
    assert seen == ["live"], seen
    assert sizes[-1] == 640, "the new level's Feed never arrived"


def test_the_menu_is_unavailable_while_the_device_is_offline_or_its_certificate_changed(own, kit_device):
    """Story 34: nothing can be written to a Device that is unreachable, or that may be an
    impostor, so the menu says why and a change is refused until it is back."""
    device, camera = kit_device
    with own.client() as api:
        assert quality_of(api, camera)["available"] is True

        device.stop()
        offline = eventually(lambda: (q := quality_of(api, camera))["reason"] and q, timeout=30)
        refused_offline = set_quality(api, camera, "low")
        device.start()
        eventually(lambda: quality_of(api, camera)["available"], timeout=30)

        device.regenerate_cert()  # a weekly rebuild, or an impostor
        rebuilt = eventually(lambda: (q := quality_of(api, camera))["reason"] and q, timeout=30)
        refused_rebuilt = set_quality(api, camera, "low")

    assert (offline["available"], offline["reason"]) == (False, "its Device is Offline")
    assert (refused_offline.status_code, refused_offline.json()["reason"]) == (409, "device_offline")
    assert (rebuilt["available"], rebuilt["reason"]) == (False, "its Device's certificate changed")
    assert (refused_rebuilt.status_code, refused_rebuilt.json()["reason"]) == (409, "certificate_changed")
    assert "size=1280x720:rate=30 " in device_config(device, "test-pattern")["runOnInit"]  # still High


def test_restarting_a_kit_device_brings_its_camera_back_at_the_stored_level(own, kit_device):
    device, camera = kit_device
    with own.client() as api:
        assert set_quality(api, camera, "minimum").status_code == 200

        device.restart()

        eventually(lambda: "size=640x360:rate=10 " in device_config(device, "test-pattern")["runOnInit"])
        time.sleep(2 * CHECK_SECONDS)
        quality = quality_of(api, camera)
    assert (quality["mode"], quality["level"], quality["changed_elsewhere"]) == ("minimum", "minimum", False)
