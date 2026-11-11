"""Seam A: adding a Camera from a raw RTSP URL, for a Device whose API is
unreachable or that was not built from the Device Kit.

    POST /api/cameras  {"device": <label>, "url": <rtsp[s]://[user:pass@]host[:port]/path>,
                        "name": <optional, default the URL path>, "fingerprint": <for rtsps>}

An RTSPS URL is trusted on first use, like adding a Device: without a
fingerprint the answer is 409 ``fingerprint_unconfirmed`` with the one the
URL's host presents.
"""

import json

import pytest

from harness.rtsp_sniffer import RtspSniffer
from harness.whep import plays, watching


def manual_form(device, label, **overrides):
    return {"device": label, "url": device.rtsps_url(), **overrides}


def test_an_rtsps_url_shows_the_fingerprint_its_host_presents_before_anything_is_added(
        instance, test_pattern_device, unique_label):
    label = unique_label("man-tofu")

    with instance.client() as api:
        response = api.post("/api/cameras", json=manual_form(test_pattern_device, label))

        assert response.status_code == 409
        assert response.json()["error"] == "fingerprint_unconfirmed"
        assert response.json()["fingerprint"] == test_pattern_device.fingerprint
        assert all(d["label"] != label for d in api.get("/api/devices").json())


def test_a_camera_added_from_an_rtsps_url_is_listed_under_a_new_device_and_plays(
        instance, test_pattern_device, unique_label):
    label = unique_label("man")
    form = manual_form(test_pattern_device, label, fingerprint=test_pattern_device.fingerprint)

    with instance.client() as api, instance.events() as events:
        response = api.post("/api/cameras", json=form)
        assert response.status_code == 201, response.text
        created = response.json()
        announced = events.next("cameras", where=lambda cs: any(c["device"] == label for c in cs))

        assert {"id": f"{label}/test-pattern", "device": label, "name": "test-pattern"}.items() <= created.items()
        assert test_pattern_device.password not in response.text
        assert [c["id"] for c in announced if c["device"] == label] == [f"{label}/test-pattern"]
        device = next(d for d in api.get("/api/devices").json() if d["label"] == label)
        assert device["api"] is False
        assert device["fingerprint"] == test_pattern_device.fingerprint
        assert test_pattern_device.password not in api.get("/api/devices").text
        with watching(api, f"{label}/test-pattern") as feed:
            assert plays(feed), feed.text


@pytest.mark.parametrize("host_case", [str.lower, str.upper])  # host names compare without case
def test_a_manual_camera_can_join_an_existing_device_and_uses_its_credentials_and_pin(
        instance, test_pattern_device, unique_label, host_case):
    label = unique_label("join")
    instance.add_device(test_pattern_device, label=label)
    host = test_pattern_device.address
    url = test_pattern_device.rtsps_url(credentials=False).replace(f"//{host}:", f"//{host_case(host)}:")

    with instance.client() as api:
        response = api.post("/api/cameras", json={"device": label, "url": url, "name": "second"})

        assert response.status_code == 201, response.text
        assert response.json()["id"] == f"{label}/second"
        with watching(api, f"{label}/second") as feed:
            assert plays(feed), feed.text


def test_a_name_already_used_on_that_device_is_refused(instance, test_pattern_device, unique_label):
    label = unique_label("twice")
    instance.add_device(test_pattern_device, label=label)

    with instance.client() as api:
        response = api.post("/api/cameras", json=manual_form(test_pattern_device, label))

        assert response.status_code == 409
        assert response.json()["error"] == "camera_exists"


def test_a_plain_rtsp_url_needs_no_fingerprint(instance, unique_label):
    label = unique_label("plain")

    with instance.client() as api:
        response = api.post("/api/cameras", json={"device": label, "url": "rtsp://192.0.2.10:8554/cam"})

        assert response.status_code == 201, response.text
        assert response.json()["id"] == f"{label}/cam"


def test_a_camera_says_whether_its_source_is_encrypted(instance, test_pattern_device, unique_label):
    """A plain rtsp:// Camera reaches the Relay in the clear; the UI marks it (ADR 0014)."""
    plain, kit = unique_label("in-clear"), unique_label("kit")
    instance.add_device(test_pattern_device, label=kit)

    with instance.client() as api:
        assert api.post("/api/cameras", json={"device": plain, "url": "rtsp://192.0.2.10:8554/cam"}).status_code == 201
        cameras = {camera["id"]: camera for camera in api.get("/api/cameras").json()}

    assert cameras[f"{plain}/cam"]["encrypted"] is False
    assert cameras[f"{kit}/test-pattern"]["encrypted"] is True


@pytest.mark.parametrize("field, value, error", [
    ("url", "http://cam.example/stream", "url_invalid"),
    ("url", "rtsps://:8322/cam", "url_invalid"),
    ("url", "rtsps://cam.example:8322/", "camera_name_invalid"),   # no name, none in the URL
    ("url", "rtsps://cam.example:8322/../../x", "camera_name_invalid"),
    ("name", "..", "camera_name_invalid"),
    ("name", "a b", "camera_name_invalid"),
    ("device", "Not_Kebab", "label_invalid"),
])
def test_unsafe_or_malformed_input_is_refused(instance, test_pattern_device, unique_label, field, value, error):
    form = manual_form(test_pattern_device, unique_label("bad"), fingerprint=test_pattern_device.fingerprint)
    form[field] = value

    with instance.client() as api:
        response = api.post("/api/cameras", json=form)

        assert response.status_code == 422, response.text
        assert response.json()["error"] == error


@pytest.mark.parametrize("url", [
    "rtsps://192.0.2.10:8322/elsewhere",   # the Device's credentials would go to this host
    "rtsp://192.0.2.10:8554/elsewhere",
])
def test_a_manual_camera_on_another_host_cannot_join_a_device(instance, test_pattern_device, unique_label, url):
    label = unique_label("hijack")
    instance.add_device(test_pattern_device, label=label)

    with instance.client() as api:
        response = api.post("/api/cameras", json={"device": label, "url": url})

        assert response.status_code == 422, response.text
        assert response.json()["error"] == "host_mismatch"
        assert [c["id"] for c in api.get("/api/cameras").json() if c["device"] == label] == \
            [f"{label}/test-pattern"]


def test_a_stored_camera_on_another_host_gets_none_of_its_devices_credentials(
        instances, test_pattern_device, unique_label):
    """A config store written before joins checked the host can still hold a
    Camera whose URL points away from its Device. The Relay must not send that
    Device's credentials there. Here the stored Device holds the test-pattern
    Device's credentials under another address, so its Camera plays only if
    they are sent to a host that is not the Device's."""
    legit, smuggled = unique_label("legacy-ok"), unique_label("legacy-bad")
    source = test_pattern_device.rtsps_url(credentials=False)

    def device(label, address):
        return {"label": label, "address": address, "username": test_pattern_device.username,
                "password": test_pattern_device.password, "fingerprint": test_pattern_device.fingerprint,
                "api": False, "cameras": [{"name": "test-pattern", "source_url": source}]}

    legacy = instances.start("legacy-store")
    legacy.stop()
    store = legacy.config_dir / "devices.json"
    store.write_text(json.dumps({"version": 1, "devices": [
        device(legit, test_pattern_device.address),
        device(smuggled, "192.0.2.10"),
    ]}))
    store.chmod(0o600)
    legacy.start()

    with legacy.client() as api:
        assert {f"{legit}/test-pattern", f"{smuggled}/test-pattern"} <= {c["id"] for c in api.get("/api/cameras").json()}
        with watching(api, f"{legit}/test-pattern") as feed:
            assert plays(feed), feed.text
        with watching(api, f"{smuggled}/test-pattern") as feed:
            assert not plays(feed), "the Relay sent the Device's credentials to another host"


def test_a_stored_plain_rtsp_camera_of_a_pinned_device_gets_none_of_its_credentials(
        instances, device_harness, instance_image, unique_label):
    """A config store written before joins required ``rtsps://`` on a pinned
    Device can hold a plain ``rtsp://`` Camera on the Device's own host. Its
    credentials would cross the network in the clear (and to whoever answers
    for that host without the pinned certificate), so the Relay pulls it
    without them. A Device that was never pinned (a manual plain-RTSP one)
    still sends its own: the control that proves the sniffer would see them."""
    pinned, unpinned = unique_label("legacy-pinned"), unique_label("legacy-plain")

    with RtspSniffer(device_harness, instance_image, unique_label("sniff")) as sniffer:
        def device(label, path, fingerprint):
            return {"label": label, "address": sniffer.address, "username": "student",
                    "password": "s3cret-" + label, "fingerprint": fingerprint, "api": False,
                    "cameras": [{"name": "cam", "source_url": sniffer.url(path)}]}

        legacy = instances.start("legacy-plain-rtsp")
        legacy.stop()
        store = legacy.config_dir / "devices.json"
        store.write_text(json.dumps({"version": 1, "devices": [
            device(pinned, "pinned", "ab" * 32),
            device(unpinned, "unpinned", ""),
        ]}))
        store.chmod(0o600)
        legacy.start()

        with legacy.client() as api:
            for label in (pinned, unpinned):
                with watching(api, f"{label}/cam") as feed:
                    assert not plays(feed)

        def authorizations(path):
            return [r["headers"]["authorization"] for r in sniffer.requests()
                    if f"/{path}" in r["line"] and "authorization" in r["headers"]]

        assert any("/pinned" in r["line"] for r in sniffer.requests()), "the Relay never pulled"
        assert authorizations("unpinned"), "control: the sniffer saw no credentials at all"
        assert authorizations("pinned") == [], "the Relay sent a pinned Device's credentials over plain RTSP"


def test_a_device_with_a_pinned_certificate_refuses_a_plain_rtsp_camera(
        instance, test_pattern_device, unique_label):
    label = unique_label("downgrade")
    instance.add_device(test_pattern_device, label=label)
    url = test_pattern_device.rtsps_url(credentials=False, scheme="rtsp", port=8554)

    with instance.client() as api:
        response = api.post("/api/cameras", json={"device": label, "url": url, "name": "plain"})

        assert response.status_code == 422, response.text
        assert response.json()["error"] == "rtsps_required"
        assert f"{label}/plain" not in [c["id"] for c in api.get("/api/cameras").json()]
