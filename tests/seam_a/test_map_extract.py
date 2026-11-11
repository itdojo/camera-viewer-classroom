"""Seam A: the instance serves a Map Extract from the config folder's ``maps`` folder (#61).

The extract is the committed fixture ``tests/fixtures/westminster.pmtiles`` (provenance in
``harness.maps``). It is served behind the session like every other route, with Range support,
since ``pmtiles.js`` reads the file a few kilobytes at a time. The browser learns where it is
from the ``settings`` event.
"""

import time

import pytest

from harness.maps import FIXTURE, start_with_extracts

EXTRACT_URL = "/api/map/extract"


@pytest.fixture(scope="module")
def with_extract(instances):
    return start_with_extracts(instances, "map-extract", {"westminster.pmtiles": (FIXTURE, time.time())})


def test_a_range_request_is_answered_with_just_those_bytes(with_extract):
    data = FIXTURE.read_bytes()

    with with_extract.client() as api:
        response = api.get(EXTRACT_URL, headers={"Range": "bytes=0-126"})
        later = api.get(EXTRACT_URL, headers={"Range": "bytes=1000-1999"})

    assert response.status_code == 206
    assert response.content == data[:127]
    assert response.content.startswith(b"PMTiles\x03")
    assert response.headers["content-range"] == f"bytes 0-126/{len(data)}"
    assert later.status_code == 206
    assert later.content == data[1000:2000]


def test_the_whole_extract_is_served_without_a_range(with_extract):
    with with_extract.client() as api:
        response = api.get(EXTRACT_URL)

    assert response.status_code == 200
    assert response.content == FIXTURE.read_bytes()
    assert response.headers["accept-ranges"] == "bytes"


def test_the_extract_needs_a_session(with_extract):
    with with_extract.client(login=False) as api:
        response = api.get(EXTRACT_URL, headers={"Range": "bytes=0-126"})

    assert response.status_code == 401
    assert b"PMTiles" not in response.content


def test_the_settings_event_says_where_the_extract_is(with_extract):
    with with_extract.events() as events:
        settings = events.next("settings")

    assert settings["map"]["extract"] == EXTRACT_URL


def test_with_online_tiles_too_the_settings_event_lists_both_basemaps(instances):
    """The extract no longer hides ``CV_MAP_TILES``: each browser chooses between them (#81)."""
    started = start_with_extracts(instances, "map-extract-and-tiles", {"westminster.pmtiles": (FIXTURE, time.time())},
                                  env={"CV_MAP_TILES": "https://tiles.invalid/{z}/{x}/{y}.png"})

    with started.events() as events:
        settings = events.next("settings")

    assert settings["map"] == {
        "extract": EXTRACT_URL,
        "tiles": {"url": "https://tiles.invalid/{z}/{x}/{y}.png", "attribution": "© OpenStreetMap contributors"},
        "imagery": None,
    }


def test_with_no_extract_the_settings_say_so_and_there_is_nothing_to_fetch(instance):
    with instance.events() as events:
        settings = events.next("settings")
    with instance.client() as api:
        response = api.get(EXTRACT_URL)

    assert settings["map"]["extract"] is None
    assert response.status_code == 404


def test_with_two_extracts_the_newest_wins_and_startup_warns(instances, tmp_path):
    older = tmp_path / "older.pmtiles"
    older.write_bytes(b"PMTiles\x03" + b"\0" * 120)  # a header's worth, never read as tiles
    now = time.time()
    started = start_with_extracts(instances, "two-extracts", {
        "aaa-older.pmtiles": (older, now - 3600),
        "zzz-newer.pmtiles": (FIXTURE, now - 60),
    })

    with started.client() as api:
        response = api.get(EXTRACT_URL)

    assert response.content == FIXTURE.read_bytes()
    warnings = [line for line in started.logs().splitlines() if "WARNING" in line and "zzz-newer.pmtiles" in line]
    assert warnings, started.logs()
    assert "aaa-older.pmtiles" in warnings[0]
