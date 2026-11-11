"""Seam A: ``CV_MAP_TILES`` names an online raster tile server, the basemap after a Map Extract (#62).

The browser learns the template and its attribution from the ``settings`` event's ``map`` field,
beside the extract. The template is operator input, so a value the map could never use (another
scheme, a missing ``{z}``/``{x}``/``{y}``, a placeholder MapLibre doesn't fill) stops the instance
at startup, with the same "refusing to start" message as every other unusable setting, and so does
an ``http://`` template while the UI is HTTPS, since browsers block it there as mixed content.
"""

import pytest

from harness.instance import run_image_once

TEMPLATE = "https://tiles.invalid/{z}/{x}/{y}.png"
OSM = "© OpenStreetMap contributors"


def test_the_settings_event_carries_the_tile_template_with_openstreetmap_attribution_by_default(instances):
    started = instances.start("map-tiles", env={"CV_MAP_TILES": TEMPLATE})

    with started.events() as events:
        settings = events.next("settings")

    assert settings["map"] == {"extract": None, "tiles": {"url": TEMPLATE, "attribution": OSM}, "imagery": None}


def test_the_operator_names_the_tile_servers_own_attribution_and_plain_http_works_without_tls(instances):
    started = instances.start("map-tiles-attribution", tls=False, env={
        "CV_MAP_TILES": "http://tiles.invalid/osm/{z}/{x}/{y}.png?key=abc",
        "CV_MAP_TILES_ATTRIBUTION": "Tiles © Example Maps, data © OpenStreetMap contributors",
    })

    with started.events() as events:
        settings = events.next("settings")

    assert settings["map"]["tiles"] == {
        "url": "http://tiles.invalid/osm/{z}/{x}/{y}.png?key=abc",
        "attribution": "Tiles © Example Maps, data © OpenStreetMap contributors",
    }


def test_without_cv_map_tiles_the_settings_name_no_tile_server(instance):
    with instance.events() as events:
        settings = events.next("settings")

    assert settings["map"]["tiles"] is None


@pytest.mark.parametrize("value", [
    "ftp://tiles.invalid/{z}/{x}/{y}.png",          # not http(s)
    "tiles.invalid/{z}/{x}/{y}.png",                 # no scheme at all
    "https:///{z}/{x}/{y}.png",                      # no host
    "https://tiles.invalid/{z}/{x}.png",             # no {y}
    "https://tiles.invalid/tiles.png",               # no placeholders
    "https://{s}.tiles.invalid/{z}/{x}/{y}.png",     # Leaflet's {s}, which MapLibre leaves as is
])
def test_an_unusable_tile_template_refuses_to_start_naming_it(instance_image, device_harness, value):
    result = run_image_once(instance_image, device_harness.session_id,
                            env={"CV_PASSWORD": "pw", "CV_MAP_TILES": value})
    output = result.stdout + result.stderr

    assert result.returncode == 2, output
    assert "refusing to start" in output
    assert "CV_MAP_TILES" in output
    assert "Traceback" not in output


def test_a_plain_http_tile_server_refuses_to_start_while_the_ui_is_https(instance_image, device_harness):
    result = run_image_once(instance_image, device_harness.session_id,
                            env={"CV_PASSWORD": "pw", "CV_MAP_TILES": "http://tiles.invalid/{z}/{x}/{y}.png"})
    output = result.stdout + result.stderr

    assert result.returncode == 2, output
    assert "refusing to start" in output
    assert "CV_MAP_TILES" in output and "https://" in output
