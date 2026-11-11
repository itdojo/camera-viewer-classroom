"""Seam A: the instance serves the UI's bundled fonts itself (#63), as fonts,
and to the login page too, which has no session yet."""

import pytest

FONTS = [
    "/fonts/fira-sans/fira-sans-regular.woff2",
    "/fonts/fira-sans/fira-sans-medium.woff2",
    "/fonts/fira-sans/fira-sans-bold.woff2",
    "/fonts/fira-sans/fira-sans-italic.woff2",
    "/fonts/fira-sans/fira-sans-bold-italic.woff2",
    "/fonts/fira-code/fira-code-variable.woff2",
]


@pytest.mark.parametrize("path", FONTS)
def test_a_bundled_font_is_served_as_woff2_without_a_session(instance, path):
    with instance.client(login=False) as api:
        response = api.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"] == "font/woff2"
    assert response.content[:4] == b"wOF2"


def test_the_font_stylesheet_is_served_without_a_session(instance):
    with instance.client(login=False) as api:
        response = api.get("/css/fonts.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert "@font-face" in response.text


def test_the_fonts_licences_still_need_a_session(instance):
    with instance.client(login=False) as api:
        response = api.get("/fonts/fira-sans/OFL.txt")

    assert response.status_code == 303
