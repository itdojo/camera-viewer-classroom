"""Seam B: a Camera pulled over plain RTSP says so (ADR 0014). Its tile carries an
"Unencrypted" badge, a kit Camera's doesn't, and the Add via RTSP form warns while
an rtsp:// URL is typed."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect


def multi_view(page: Page):
    return page.get_by_role("region", name="Multi View")


def connect(page: Page, label: str) -> None:
    page.get_by_role("list", name="Cameras").get_by_role("checkbox", name=re.compile(rf"\b{label}\b")).check()


def tile_of(page: Page, label: str):
    return multi_view(page).get_by_role("article", name=re.compile(rf"\b{label}\b"))


def test_a_plain_rtsp_camera_is_marked_unencrypted_on_its_tile_and_a_kit_camera_is_not(
        page, instance, test_pattern_device, unique_label):
    plain, kit = unique_label("in-clear"), unique_label("kit-tile")
    instance.add_device(test_pattern_device, label=kit)
    with instance.client() as api:
        assert api.post("/api/cameras", json={"device": plain, "url": "rtsp://192.0.2.10:8554/cam"}).status_code == 201
    try:
        page.goto(instance.base_url)
        connect(page, plain)
        connect(page, kit)

        badge = tile_of(page, plain).get_by_text("Unencrypted", exact=True)
        expect(badge).to_be_visible()
        expect(badge).to_have_attribute("title", re.compile("plain RTSP"))
        expect(tile_of(page, kit)).to_be_visible()
        expect(tile_of(page, kit).get_by_text("Unencrypted", exact=True)).to_have_count(0)
    finally:
        with instance.client() as api:
            api.delete(f"/api/devices/{plain}")


def test_the_add_via_rtsp_form_warns_while_a_plain_rtsp_url_is_typed(page, instance):
    page.goto(instance.base_url)
    form = page.get_by_role("form", name="Add via RTSP")
    warning = form.get_by_text(re.compile("^Plain RTSP is unencrypted"))
    url = form.get_by_role("textbox", name="RTSP URL")

    expect(warning).to_be_hidden()
    url.fill("rtsp://192.0.2.10:8554/cam")
    expect(warning).to_be_visible()
    url.fill("rtsps://192.0.2.10:8322/cam")
    expect(warning).to_be_hidden()
