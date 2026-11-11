"""Seam A: a browser that asks the HTTPS port for plain HTTP gets a 301 to the
same host and path over https:// (ADR 0014), not an empty reply. Edge cases
of the listener itself are in ``tests/unit/test_ui_listener.py``."""

import httpx


def test_plain_http_on_the_ui_port_is_redirected_to_https(instance):
    plain = instance.base_url.replace("https://", "http://", 1)

    with httpx.Client(timeout=5) as client:
        response = client.get(plain + "/login?from=bookmark")

    assert response.status_code == 301
    assert response.headers["location"] == instance.base_url + "/login?from=bookmark"


def test_the_redirect_needs_no_session_and_reveals_nothing(instance):
    plain = instance.base_url.replace("https://", "http://", 1)

    with httpx.Client(timeout=5) as client:
        response = client.get(plain + "/api/devices")

    assert response.status_code == 301
    assert response.headers["location"] == instance.base_url + "/api/devices"
    assert "set-cookie" not in response.headers
    assert "label" not in response.text
