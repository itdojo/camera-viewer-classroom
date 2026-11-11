"""Seam A: Device labels are kebab-case and unique, checked by the instance itself.

The UI checks the pattern too, but a label ends up in Relay path names and
Capture file names, so the API never trusts the browser to have done it.
"""

import pytest


def device_form(device, label, **overrides):
    return {
        "address": device.address,
        "label": label,
        "username": device.username,
        "password": device.password,
        "fingerprint": device.fingerprint,
        **overrides,
    }


def labels(api):
    return [d["label"] for d in api.get("/api/devices").json()]


@pytest.mark.parametrize("label", [
    "Cam2",            # upper case
    "cam_2",           # underscore
    "cam 2",           # space
    "-cam",            # leading hyphen
    "cam-",            # trailing hyphen
    "cam--2",          # empty segment
    "",                # empty
    "../config",       # path traversal
    "cam/2",           # path separator
    "a" * 64,          # too long for a file name prefix
])
def test_a_label_that_is_not_kebab_case_is_refused_with_a_clear_message(instance, test_pattern_device, label):
    with instance.client() as api:
        response = api.post("/api/devices", json=device_form(test_pattern_device, label))

        assert response.status_code == 422
        body = response.json()
        assert body["error"] == "label_invalid"
        assert "kebab-case" in body["message"]
        assert label not in labels(api)


def test_a_label_already_in_use_is_refused_with_a_clear_message(instance, test_pattern_device, unique_label):
    label = unique_label("dup")
    instance.add_device(test_pattern_device, label=label)

    with instance.client() as api:
        response = api.post("/api/devices", json=device_form(test_pattern_device, label))

        assert response.status_code == 409
        body = response.json()
        assert body["error"] == "label_taken"
        assert label in body["message"]
        assert labels(api).count(label) == 1
