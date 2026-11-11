"""Seam A: no request body bigger than the cap is read into memory, signed in or not.

A WHEP offer from a signed-in browser and a login attempt from anyone are the two ways a body
reaches the backend; both are refused at the cap before their route runs, and the instance
keeps serving.
"""

import pytest

from camera_viewer.body_limit import MAX_BODY_BYTES
from harness.whep import plays, watching

TOO_BIG = b"a=x\r\n" * (MAX_BODY_BYTES // 5 + 1)


def chunked():  # no Content-Length: the server has to count as it reads
    for start in range(0, len(TOO_BIG), 4096):
        yield TOO_BIG[start:start + 4096]


@pytest.mark.parametrize("send", [lambda: TOO_BIG, chunked], ids=["declared", "chunked"])
def test_an_oversized_whep_offer_is_refused_and_every_feed_keeps_playing(
        instance, test_pattern_device, unique_label, send):
    label = unique_label("whep-big")
    instance.add_device(test_pattern_device, label=label)
    camera = f"{label}/test-pattern"

    with instance.client() as api:
        refused = api.post(f"/api/feeds/{camera}/whep", content=send(),
                           headers={"Content-Type": "application/sdp"})
        with watching(api, camera) as answer:
            assert plays(answer)

    assert refused.status_code == 413, refused.text
    assert refused.json()["error"] == "body_too_large"  # the gate's answer, not the Relay's
    assert refused.json()["max_bytes"] == MAX_BODY_BYTES


@pytest.mark.parametrize("send", [lambda: TOO_BIG, chunked], ids=["declared", "chunked"])
def test_an_oversized_login_attempt_is_refused_without_a_session(instance, send):
    with instance.client(login=False) as anonymous:
        refused = anonymous.post("/api/session", content=send(),
                                 headers={"Content-Type": "application/json"})
        logged_in = anonymous.post("/api/session", json={"password": instance.password})

    assert refused.status_code == 413, refused.text
    assert refused.json()["error"] == "body_too_large"
    assert logged_in.status_code == 200
