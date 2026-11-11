"""Seam A: adding a Device — trust on first use, then discovery."""


def device_form(device, label, **overrides):
    return {
        "address": device.address,
        "label": label,
        "username": device.username,
        "password": device.password,
        **overrides,
    }


def labels(api):
    return [d["label"] for d in api.get("/api/devices").json()]


def test_adding_a_device_without_a_fingerprint_shows_the_fingerprint_it_presents(
        instance, test_pattern_device, unique_label):
    label = unique_label("tofu")

    with instance.client() as api:
        response = api.post("/api/devices", json=device_form(test_pattern_device, label))

        assert response.status_code == 409
        assert response.json()["error"] == "fingerprint_unconfirmed"
        assert response.json()["fingerprint"] == test_pattern_device.fingerprint
        assert label not in labels(api)


def test_adding_a_device_lists_its_camera_and_announces_it_on_the_event_stream(
        instance, test_pattern_device, unique_label):
    label = unique_label("tp")
    expected = {
        "id": f"{label}/test-pattern",
        "device": label,
        "name": "test-pattern",
        "resolution": {"width": 1280, "height": 720},
    }

    with instance.events() as events:
        created = instance.add_device(test_pattern_device, label=label)
        announced = events.next("cameras", where=lambda cameras: any(c["device"] == label for c in cameras))

    assert created["label"] == label
    assert created["fingerprint"] == test_pattern_device.fingerprint
    assert "password" not in created
    assert expected.items() <= next(c for c in announced if c["device"] == label).items()
    with instance.client() as api:
        listed = [c for c in api.get("/api/cameras").json() if c["device"] == label]
        assert len(listed) == 1 and expected.items() <= listed[0].items()
        assert label in labels(api)


def test_a_fingerprint_the_device_does_not_present_is_refused(instance, test_pattern_device, unique_label):
    label = unique_label("pin")

    with instance.client() as api:
        response = api.post("/api/devices", json=device_form(test_pattern_device, label, fingerprint="ab" * 32))

        assert response.status_code == 409
        assert response.json()["error"] == "fingerprint_mismatch"
        assert response.json()["fingerprint"] == test_pattern_device.fingerprint
        assert label not in labels(api)


def test_the_fingerprint_may_be_pasted_with_colons_in_upper_case(instance, test_pattern_device, unique_label):
    fingerprint = test_pattern_device.fingerprint.upper()
    pasted = ":".join(fingerprint[i:i + 2] for i in range(0, len(fingerprint), 2))

    created = instance.add_device(test_pattern_device, label=unique_label("paste"), fingerprint=pasted)

    assert created["fingerprint"] == test_pattern_device.fingerprint


def test_wrong_credentials_are_refused_before_anything_is_added(instance, test_pattern_device, unique_label):
    label = unique_label("creds")
    form = device_form(test_pattern_device, label, password="wrong", fingerprint=test_pattern_device.fingerprint)

    with instance.client() as api:
        response = api.post("/api/devices", json=form)

        assert response.status_code == 422
        assert response.json()["error"] == "device_credentials_rejected"
        assert label not in labels(api)


def test_an_unreachable_device_is_reported_as_such(instance, test_pattern_device, unique_label):
    label = unique_label("gone")
    form = device_form(test_pattern_device, label, address="no-such-device")

    with instance.client() as api:
        response = api.post("/api/devices", json=form)

        assert response.status_code == 502
        assert response.json()["error"] == "device_unreachable"
        assert label not in labels(api)
