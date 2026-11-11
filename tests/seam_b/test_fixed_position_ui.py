"""Seam B: an instructor gives a Device a Fixed Position in the Devices Panel, and Multi View tiles and
the Single View header show it as fixed, offline (#58).

The Device is the shared test-pattern one (no GPS Sidecar) under a label of each test's own, so its
only Position is the Fixed Position. The browser can reach the instance and nothing else
(``harness.offline``).
"""

import re

import pytest
from playwright.sync_api import expect

HOME = {"latitude": 51.47793, "longitude": -0.00148}
COORDINATES = "51.47793, -0.00148"


@pytest.fixture
def device(instance, test_pattern_device, unique_label):
    label = unique_label("fixed-ui")
    instance.add_device(test_pattern_device, label=label)
    yield label
    with instance.client() as api:
        api.delete(f"/api/devices/{label}")


def device_entry(page, label):
    return page.get_by_role("list", name="Devices").get_by_role("listitem").filter(
        has=page.get_by_text(label, exact=True))


def position_summary(page, label):
    """The line in ``label``'s entry of the Devices Panel that reads "Position", its source and
    its coordinates, and opens the Fixed Position form."""
    return device_entry(page, label).locator("summary")


def fixed_position_form(page, label):
    """The Fixed Position form in ``label``'s entry of the Devices Panel, opened."""
    entry = device_entry(page, label)
    entry.get_by_text("Position", exact=True).click()
    form = entry.get_by_role("form", name=f"Fixed Position of {label}")
    expect(form).to_be_visible()
    return form


def tile_position(page, label):
    page.get_by_role("list", name="Cameras").get_by_role("checkbox", name=re.compile(rf"\b{label}\b")).check()
    tile = page.get_by_role("region", name="Multi View").get_by_role("article", name=re.compile(rf"\b{label}\b"))
    return tile.get_by_role("group", name="Position")


def test_the_device_panel_sets_a_fixed_position_the_tile_shows_it_as_fixed_and_clearing_it_removes_it(
        offline, instance, device):
    page = offline.new_page()
    page.goto(instance.base_url)
    position = tile_position(page, device)
    fix = position.locator("[data-fix]")
    expect(fix).to_have_attribute("data-fix", "none")
    summary = position_summary(page, device)
    expect(summary).to_have_text("PositionNone")

    form = fixed_position_form(page, device)
    form.get_by_label("Latitude").fill(str(HOME["latitude"]))
    form.get_by_label("Longitude").fill(str(HOME["longitude"]))
    form.get_by_role("button", name="Save").click()

    expect(fix).to_have_attribute("data-fix", "fixed")
    expect(fix).to_have_text("Fixed")
    expect(fix).to_have_attribute("title", "Fixed Position, entered by hand")
    expect(position.get_by_text(COORDINATES, exact=True)).to_be_visible()
    expect(position).not_to_have_attribute("data-stale", "")
    expect(summary).to_have_text(f"PositionFixed{COORDINATES}")

    form.get_by_role("button", name="Clear").click()

    expect(fix).to_have_attribute("data-fix", "none")
    expect(summary).to_have_text("PositionNone")
    expect(form.get_by_label("Latitude")).to_have_value("")
    assert offline.blocked == []


def test_the_panel_shows_the_servers_refusal_of_an_impossible_fixed_position(offline, instance, device):
    page = offline.new_page()
    page.goto(instance.base_url)
    form = fixed_position_form(page, device)

    form.get_by_label("Latitude").fill("91")
    form.get_by_label("Longitude").fill("0")
    form.get_by_role("button", name="Save").click()

    expect(form.get_by_role("alert")).to_contain_text("latitude is out of range")
    form.get_by_label("Latitude").fill("north")
    form.get_by_role("button", name="Save").click()
    expect(form.get_by_role("alert")).to_contain_text("latitude is not a number")
    with instance.client() as api:
        assert api.delete(f"/api/devices/{device}/fixed-position").status_code == 204  # nothing was set
    assert offline.blocked == []


def test_the_field_shows_the_fixed_position_already_set_and_keeps_what_is_typed_while_the_list_redraws(
        offline, instance, device, test_pattern_device, unique_label):
    with instance.client() as api:
        assert api.put(f"/api/devices/{device}/fixed-position", json=HOME).status_code == 200
    page = offline.new_page()
    page.goto(instance.base_url)
    form = fixed_position_form(page, device)
    expect(form.get_by_label("Latitude")).to_have_value(str(HOME["latitude"]))
    expect(form.get_by_label("Longitude")).to_have_value(str(HOME["longitude"]))

    form.get_by_label("Latitude").fill("-33.85681")
    other = unique_label("fixed-ui-other")
    instance.add_device(test_pattern_device, label=other)  # re-publishes `devices`: the list redraws
    expect(page.get_by_role("list", name="Devices").get_by_text(other, exact=True)).to_be_visible()

    expect(form.get_by_label("Latitude")).to_have_value("-33.85681")
    expect(form.get_by_label("Latitude")).to_be_focused()
    with instance.client() as api:
        api.delete(f"/api/devices/{other}")
    assert offline.blocked == []


def test_the_single_view_header_shows_a_fixed_position_as_fixed(offline, instance, device):
    with instance.client() as api:
        assert api.put(f"/api/devices/{device}/fixed-position", json=HOME).status_code == 200
    page = offline.new_page()
    page.goto(instance.base_url)

    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(rf"\b{device}\b")).click()

    position = page.get_by_role("region", name="Single View").locator("header").get_by_role("group", name="Position")
    expect(position).to_be_visible()
    expect(position).to_have_attribute("data-fixed", "")
    expect(position.get_by_text(COORDINATES, exact=True)).to_be_visible()
    expect(position).to_contain_text("Fixed Position")
    expect(position).not_to_contain_text("km/h")
    assert offline.blocked == []
