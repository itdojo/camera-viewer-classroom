"""Seam A: the frontend is revalidated on every load, so a browser never runs a
cached module from an older release against a newer page."""

import pytest

FRONTEND = ["/", "/index.html", "/js/main.js", "/js/devices.js", "/css/tokens.css", "/setup.html", "/js/setup.js"]


@pytest.mark.parametrize("path", FRONTEND)
def test_frontend_files_must_be_revalidated(instance, path):
    with instance.client() as api:
        response = api.get(path)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_an_unchanged_file_revalidates_without_a_body(instance):
    with instance.client() as api:
        etag = api.get("/js/main.js").headers["etag"]
        again = api.get("/js/main.js", headers={"If-None-Match": etag})

    assert again.status_code == 304
    assert again.headers["cache-control"] == "no-cache"
