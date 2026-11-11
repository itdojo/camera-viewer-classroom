"""Seam A: one shared password per instance, and a session on every route."""

import os
import subprocess
import time

import pytest

from harness.instance import COMPOSE, run_image_once


@pytest.mark.parametrize("env", [{}, {"CV_PASSWORD": ""}, {"CV_PASSWORD": "   "}],
                         ids=["unset", "empty", "blank"])
def test_the_instance_refuses_to_start_without_a_password(instance_image, device_harness, env):
    result = run_image_once(instance_image, device_harness.session_id, env=env)

    assert result.returncode != 0
    assert "CV_PASSWORD" in result.stdout + result.stderr


def test_compose_refuses_to_bring_up_an_instance_without_a_password(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "CV_PASSWORD"}
    no_dotenv = tmp_path / "empty.env"  # ignore any .env a developer keeps next to compose.yaml
    no_dotenv.write_text("")

    result = subprocess.run(["docker", "compose", "--env-file", str(no_dotenv), "-f", str(COMPOSE),
                             "config", "-q"], env=env, capture_output=True, text=True, timeout=60)

    assert result.returncode != 0
    assert "CV_PASSWORD" in result.stderr


# -- every route behind the session -------------------------------------------

def api_routes(instance):
    """Every (method, path) the instance's API declares, read from its own
    OpenAPI document, so a route added later is covered without editing this
    test. Path parameters are filled with placeholders: the session check
    must answer before any route looks at them."""
    with instance.client() as api:
        document = api.get("/openapi.json").json()
    routes = []
    for path, operations in document["paths"].items():
        for method in operations:
            filled = path
            while "{" in filled:
                start = filled.index("{")
                filled = filled[:start] + "probe" + filled[filled.index("}", start) + 1:]
            routes.append((method.upper(), filled))
    return routes


def assert_unauthenticated(response):
    assert response.status_code == 401, (response.request.method, response.request.url, response.text)
    assert response.json()["error"] == "unauthenticated"


def test_the_route_list_covers_devices_cameras_feeds_and_events(instance):
    paths = {path for _, path in api_routes(instance)}

    assert {"/api/devices", "/api/cameras", "/api/events", "/api/feeds/probe/whep"} <= paths


def test_every_api_route_rejects_a_request_without_a_session(instance):
    with instance.client(login=False) as anonymous:
        for method, path in api_routes(instance):
            if path == "/api/session":
                continue  # logging in is the one thing allowed without a session
            assert_unauthenticated(anonymous.request(method, path))


def test_a_forged_session_cookie_is_rejected(instance):
    with instance.client() as api:
        name = next(iter(api.cookies))
    with instance.client(login=False, cookies={name: "4102444800.forged.00"}) as forger:
        assert_unauthenticated(forger.get("/api/cameras"))


def test_the_event_stream_rejects_a_browser_without_a_session(instance):
    with instance.events(expect=None, login=False) as stream:
        assert stream.status_code == 401


def test_whep_signalling_rejects_an_offer_without_a_session(instance, test_pattern_device, unique_label):
    label = unique_label("whep")
    instance.add_device(test_pattern_device, label=label)

    with instance.client(login=False) as anonymous:
        response = anonymous.post(f"/api/feeds/{label}/test-pattern/whep", content=b"v=0\r\n",
                                  headers={"Content-Type": "application/sdp"})
        assert_unauthenticated(response)
        assert_unauthenticated(anonymous.delete(f"/api/feeds/{label}/test-pattern/whep/some-session"))


@pytest.mark.parametrize("path", ["/", "/index.html", "/js/main.js", "/js/whep.js", "/no-such-page"])
def test_without_a_session_every_page_and_script_sends_the_browser_to_the_login_page(instance, path):
    with instance.client(login=False) as anonymous:
        response = anonymous.get(path)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_login_page_and_only_its_own_files_load_without_a_session(instance):
    with instance.client(login=False) as anonymous:
        page = anonymous.get("/login")
        assert page.status_code == 200
        assert 'type="password"' in page.text
        for asset in ("/css/tokens.css", "/css/app.css", "/js/login.js"):
            assert anonymous.get(asset).status_code == 200, asset


def test_a_logged_in_browser_skips_the_login_page(instance):
    with instance.client() as api:
        response = api.get("/login")

    assert response.status_code == 303
    assert response.headers["location"] == "/"


# -- logging in and out -------------------------------------------------------

def test_the_right_password_opens_a_session(instance):
    with instance.client(login=False) as api:
        response = api.post("/api/session", json={"password": instance.password})

        assert response.status_code == 200
        assert api.get("/api/cameras").status_code == 200
        assert api.get("/api/session").json() == {"authenticated": True}


def test_a_wrong_password_opens_nothing(instance):
    with instance.client(login=False) as api:
        response = api.post("/api/session", json={"password": instance.password + "x"})

        assert response.status_code == 401
        assert response.json()["error"] == "wrong_password"
        assert_unauthenticated(api.get("/api/cameras"))
        assert api.get("/api/session").json() == {"authenticated": False}


def test_the_session_cookie_is_http_only_and_same_site(instance):
    with instance.client(login=False) as api:
        set_cookie = api.post("/api/session", json={"password": instance.password}).headers["set-cookie"]

    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()


def test_logging_out_ends_the_session_even_for_a_copied_cookie(instance):
    with instance.client() as api:
        copied = dict(api.cookies)
        assert api.delete("/api/session").status_code == 204

        assert_unauthenticated(api.get("/api/cameras"))
    with instance.client(login=False, cookies=copied) as replay:
        assert_unauthenticated(replay.get("/api/cameras"))


def test_the_logout_cookie_carries_the_same_flags_as_the_login_cookie(instance):
    """A browser may ignore a Set-Cookie that clears a Secure cookie without Secure itself."""
    with instance.client() as api:
        cleared = api.delete("/api/session").headers["set-cookie"].lower()

    assert "secure" in cleared
    assert "httponly" in cleared
    assert "samesite=strict" in cleared


def test_a_session_survives_an_instance_restart(instances, unique_label):
    own = instances.start(unique_label("auth-restart"))
    cookies = own.session_cookies()

    own.restart()

    with own.client(login=False, cookies=cookies) as api:
        assert api.get("/api/cameras").status_code == 200


def test_changing_the_password_ends_every_session(instances, unique_label):
    own = instances.start(unique_label("auth-rekey"))
    cookies = own.session_cookies()

    own.stop()
    own.env["CV_PASSWORD"] = "a-new-team-password"
    own.start()

    with own.client(login=False, cookies=cookies) as api:
        assert_unauthenticated(api.get("/api/cameras"))
    with own.client() as api:
        assert api.get("/api/cameras").status_code == 200


# -- guessing the shared password ----------------------------------------------

def test_repeated_wrong_passwords_lock_that_client_out_for_a_while(instances, unique_label):
    own = instances.start(unique_label("auth-throttle"),
                          env={"CV_LOGIN_MAX_FAILURES": "3", "CV_LOGIN_LOCKOUT_SECONDS": "3"})

    with own.client(login=False) as api:
        wrong = [api.post("/api/session", json={"password": "guess"}).status_code for _ in range(3)]
        locked = api.post("/api/session", json={"password": own.password})  # even the right one, for now
        assert wrong == [401, 401, 401]
        assert locked.status_code == 429, locked.text
        assert locked.json()["error"] == "too_many_attempts"
        assert 1 <= int(locked.headers["retry-after"]) <= 3
        assert_unauthenticated(api.get("/api/cameras"))

        time.sleep(int(locked.headers["retry-after"]) + 0.5)
        assert api.post("/api/session", json={"password": own.password}).status_code == 200
        assert api.get("/api/cameras").status_code == 200
