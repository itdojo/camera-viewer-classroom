"""Seam A: a rebuilt Device's new certificate is reported, never trusted silently, and re-pinned on request."""

import time

import pytest

from harness.whep import plays, watching

DETECTED_WITHIN = 30  # seconds; the instance checks every Device's certificate every few seconds


@pytest.fixture
def pinned_device(instance, device_harness, unique_label):
    """A Device of its own (so it can be rebuilt), added to the shared instance and pinned.
    Removed afterwards, from the instance and the Docker host: a long Camera list on the
    shared instance pushes Seam B's viewer off screen, and every test-pattern Device
    encodes video nonstop."""
    device = device_harness.add_device(unique_label("rebuilt"))
    instance.add_device(device)
    yield device
    with instance.client() as api:
        api.delete(f"/api/devices/{device.label}")
    device_harness.remove_device(device)


def device_view(api, label):
    return next(d for d in api.get("/api/devices").json() if d["label"] == label)


def camera_views(api, label):
    return [c for c in api.get("/api/cameras").json() if c["device"] == label]


def test_a_rebuilt_device_is_reported_as_certificate_changed_with_its_new_fingerprint(instance, pinned_device):
    device = pinned_device
    pinned = device.fingerprint

    with instance.events() as events:
        new = device.regenerate_cert()  # a weekly rebuild
        assert new != pinned
        announced = events.next("devices", timeout=DETECTED_WITHIN, where=lambda devices: any(
            d["label"] == device.label and d["certificate_changed"] for d in devices))
        cameras = events.next("cameras", timeout=DETECTED_WITHIN, where=lambda cameras: any(
            c["device"] == device.label and c["certificate_changed"] for c in cameras))

    mine = next(d for d in announced if d["label"] == device.label)
    assert mine["fingerprint"] == pinned  # still pinned to the old one: nothing was trusted
    assert mine["presented_fingerprint"] == new
    assert [c["name"] for c in cameras if c["device"] == device.label] == ["test-pattern"]
    with instance.client() as api:
        assert device_view(api, device.label)["presented_fingerprint"] == new
        assert all(c["certificate_changed"] for c in camera_views(api, device.label))


def test_a_device_whose_certificate_has_not_changed_is_not_flagged(instance, test_pattern_device, unique_label):
    label = unique_label("steady")
    instance.add_device(test_pattern_device, label=label)
    time.sleep(6)  # longer than a check interval

    with instance.client() as api:
        view = device_view(api, label)
        assert view["certificate_changed"] is False
        assert view["presented_fingerprint"] == test_pattern_device.fingerprint
        assert [c["certificate_changed"] for c in camera_views(api, label)] == [False]


def wait_until_changed(instance, label):
    deadline = time.monotonic() + DETECTED_WITHIN
    with instance.client() as api:
        while time.monotonic() < deadline:
            if device_view(api, label)["certificate_changed"]:
                return
            time.sleep(0.5)
    raise AssertionError(f"{label} was never reported certificate changed")


def repin(api, label, fingerprint=None):
    body = {} if fingerprint is None else {"fingerprint": fingerprint}
    return api.put(f"/api/devices/{label}/fingerprint", json=body)


def test_re_pinning_to_the_presented_fingerprint_trusts_the_rebuilt_device(instance, pinned_device):
    device = pinned_device
    new = device.regenerate_cert()
    wait_until_changed(instance, device.label)

    with instance.events() as events, instance.client() as api:
        response = repin(api, device.label, ":".join(new[i:i + 2] for i in range(0, 64, 2)).upper())
        cameras = events.next("cameras", where=lambda cameras: any(
            c["device"] == device.label and not c["certificate_changed"] for c in cameras))

        assert response.status_code == 200
        assert response.json()["fingerprint"] == new
        assert response.json()["certificate_changed"] is False
        assert device_view(api, device.label)["fingerprint"] == new
        assert [c["name"] for c in cameras if c["device"] == device.label] == ["test-pattern"]
        with watching(api, f"{device.label}/test-pattern") as feed:
            assert plays(feed)


def test_re_pinning_without_confirming_a_fingerprint_shows_the_presented_one_and_changes_nothing(
        instance, pinned_device):
    device = pinned_device
    pinned = device.fingerprint
    new = device.regenerate_cert()
    wait_until_changed(instance, device.label)

    with instance.client() as api:
        response = repin(api, device.label)

        assert response.status_code == 409
        assert response.json()["error"] == "fingerprint_unconfirmed"
        assert response.json()["fingerprint"] == new
        assert device_view(api, device.label)["fingerprint"] == pinned


def test_re_pinning_to_a_fingerprint_the_device_does_not_present_is_refused(instance, pinned_device):
    device = pinned_device
    pinned = device.fingerprint
    new = device.regenerate_cert()
    wait_until_changed(instance, device.label)

    with instance.client() as api:
        with watching(api, f"{device.label}/test-pattern") as feed:
            assert not plays(feed)  # the Relay refuses the new certificate too
        response = repin(api, device.label, "ab" * 32)

        assert response.status_code == 409
        assert response.json()["error"] == "fingerprint_mismatch"
        assert response.json()["fingerprint"] == new
        assert device_view(api, device.label)["fingerprint"] == pinned
        assert device_view(api, device.label)["certificate_changed"] is True


def test_re_pinning_an_unknown_device_is_not_found(instance, unique_label):
    with instance.client() as api:
        response = repin(api, unique_label("nobody"), "ab" * 32)

    assert response.status_code == 404
    assert response.json()["error"] == "device_not_found"


def test_a_changed_certificate_is_never_trusted_by_waiting(instance, pinned_device):
    device = pinned_device
    pinned = device.fingerprint
    device.regenerate_cert()
    wait_until_changed(instance, device.label)

    time.sleep(12)  # several check intervals and Relay reconciles

    with instance.client() as api:
        view = device_view(api, device.label)
        assert view["fingerprint"] == pinned
        assert view["certificate_changed"] is True


def test_a_re_pin_survives_an_instance_restart(instances, device_harness, unique_label):
    own = instances.start(unique_label("repin-restart"))
    device = device_harness.add_device(unique_label("rebuilt"))
    own.add_device(device)
    new = device.regenerate_cert()
    with own.client() as api:
        assert repin(api, device.label, new).status_code == 200

    own.restart()

    with own.client() as api:
        view = device_view(api, device.label)
        assert view["fingerprint"] == new
        with watching(api, f"{device.label}/test-pattern") as feed:
            assert plays(feed)
    instances.remove(own)
    device_harness.remove_device(device)


def test_re_pinning_needs_a_session(instance, test_pattern_device, unique_label):
    label = unique_label("gated")
    instance.add_device(test_pattern_device, label=label)

    with instance.client(login=False) as api:
        response = repin(api, label, test_pattern_device.fingerprint)

    assert response.status_code == 401
