"""Seam A: ``CV_MAP_IMAGERY`` names a satellite imagery server, the Satellite Basemap (#83).

It works as ``CV_MAP_TILES`` does (``test_map_tiles.py``): the browser learns the template and its
attribution from the ``settings`` event's ``map`` field, and a template the map could never use
stops the instance at startup with the same rules and messages, naming ``CV_MAP_IMAGERY``. There is
no default provider, so without the variable the settings name no imagery server, and there is no
provider to credit by default either: an imagery server with no ``CV_MAP_IMAGERY_ATTRIBUTION`` is
credited by its host name, so the map never shows imagery with nothing saying where it came from.
"""

import pytest

from harness.instance import run_image_once

TEMPLATE = "https://imagery.invalid/tile/{z}/{y}/{x}"
ATTRIBUTION = "Imagery © Example Imaging"


def test_the_settings_event_carries_the_imagery_template_and_its_attribution(instances):
    started = instances.start("map-imagery", env={"CV_MAP_IMAGERY": TEMPLATE,
                                                  "CV_MAP_IMAGERY_ATTRIBUTION": ATTRIBUTION})

    with started.events() as events:
        settings = events.next("settings")

    assert settings["map"] == {"extract": None, "tiles": None,
                               "imagery": {"url": TEMPLATE, "attribution": ATTRIBUTION}}


def test_imagery_with_no_attribution_is_credited_by_its_host_and_plain_http_works_without_tls(instances):
    started = instances.start("map-imagery-host", tls=False,
                              env={"CV_MAP_IMAGERY": "http://imagery.invalid:8080/{z}/{x}/{y}.jpg?key=abc"})

    with started.events() as events:
        settings = events.next("settings")

    assert settings["map"]["imagery"] == {"url": "http://imagery.invalid:8080/{z}/{x}/{y}.jpg?key=abc",
                                          "attribution": "Imagery: imagery.invalid"}


def test_without_cv_map_imagery_the_settings_name_no_imagery_server(instance):
    with instance.events() as events:
        settings = events.next("settings")

    assert settings["map"]["imagery"] is None


@pytest.mark.parametrize("value", [
    "ftp://imagery.invalid/{z}/{x}/{y}.jpg",          # not http(s)
    "imagery.invalid/{z}/{x}/{y}.jpg",                 # no scheme at all
    "https:///{z}/{x}/{y}.jpg",                        # no host
    "https://imagery.invalid/{z}/{x}.jpg",             # no {y}
    "https://imagery.invalid/imagery.jpg",             # no placeholders
    "https://{s}.imagery.invalid/{z}/{x}/{y}.jpg",     # Leaflet's {s}, which MapLibre leaves as is
])
def test_an_unusable_imagery_template_refuses_to_start_naming_it(instance_image, device_harness, value):
    result = run_image_once(instance_image, device_harness.session_id,
                            env={"CV_PASSWORD": "pw", "CV_MAP_IMAGERY": value})
    output = result.stdout + result.stderr

    assert result.returncode == 2, output
    assert "refusing to start" in output
    assert "CV_MAP_IMAGERY" in output
    assert "CV_MAP_TILES" not in output
    assert "Traceback" not in output


def test_a_plain_http_imagery_server_refuses_to_start_while_the_ui_is_https(instance_image, device_harness):
    result = run_image_once(instance_image, device_harness.session_id,
                            env={"CV_PASSWORD": "pw", "CV_MAP_IMAGERY": "http://imagery.invalid/{z}/{x}/{y}.jpg"})
    output = result.stdout + result.stderr

    assert result.returncode == 2, output
    assert "refusing to start" in output
    assert "CV_MAP_IMAGERY" in output and "https://" in output
