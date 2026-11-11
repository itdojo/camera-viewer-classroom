"""Seam A: the Device setup guide (#100) is a page like the UI itself, behind
the session, so it needs no `auth.PUBLIC` entry."""

import pytest


@pytest.mark.parametrize("path", ["/setup.html", "/js/setup.js"])
def test_the_setup_guide_needs_a_session(instance, path):
    with instance.client(login=False) as api:
        response = api.get(path)

    assert response.status_code == 303
