"""Seam B helpers for Map View and the Map Preview, shared by ``test_map_view.py`` and the
``test_map_preview*.py`` modules. The ``placed`` fixture over them is in ``tests/seam_b/conftest.py``.

Map View markers are buttons named after their Device and its Position ("<label>, <address>:
<lat>, <lon>, …"), so ``marker`` finds one by its label alone."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

HERE = {"latitude": 51.52000, "longitude": -0.14000}
EAST = {"latitude": 51.52000, "longitude": -0.10000}


def place(instance, label: str, where: dict) -> None:
    """Give the Device a Fixed Position, so its marker appears at once with no wait for a fix."""
    with instance.client() as api:
        assert api.put(f"/api/devices/{label}/fixed-position", json=where).status_code == 200


def address_of(instance, label: str) -> str:
    """The Device's address, which follows its label in its marker's name."""
    with instance.client() as api:
        return next(d["address"] for d in api.get("/api/devices").json() if d["label"] == label)


def map_view(page: Page):
    return page.get_by_role("region", name="Map View")


def marker(page: Page, label: str):
    return map_view(page).get_by_role("button", name=re.compile(rf"^{label}(, \S+)?: "))


def close_bar(page: Page) -> None:
    """Close the harness-seeded Management Bar, which lies over the left of the map and takes
    the first Esc."""
    page.get_by_role("button", name="Management Bar").click()
    expect(page.get_by_role("complementary", name="Management Bar")).to_be_hidden()


def open_map_view(page: Page, instance, labels, *, bar_open: bool = False, phone: bool = False) -> None:
    """Load the app, switch to Map View and wait for each label's marker. The Management Bar is
    closed unless ``bar_open``. On a ``phone`` the switch is tapped, and the bar is left alone,
    since at phone width it sits below the viewer, always shown, with no toggle."""
    page.goto(instance.base_url)
    switch = page.get_by_role("banner").get_by_role("group", name="View").get_by_role("button", name="Map View")
    if phone:
        switch.tap()
    else:
        switch.click()
        if not bar_open:
            close_bar(page)
    for label in labels:
        expect(marker(page, label)).to_be_visible(timeout=20_000)
