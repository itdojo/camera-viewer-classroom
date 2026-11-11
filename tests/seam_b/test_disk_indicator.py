"""Seam B: the disk indicator. Every page shows free and total space for
the captures folder, warns when it runs low, and says plainly when new
Recordings and Snapshots are refused. Levels are triggered, as in Seam A, by
setting a threshold above the real free space."""

import re
import secrets

import pytest
from playwright.sync_api import expect

SIZE = r"\d+(\.\d)? (kB|MB|GB|TB)"


@pytest.fixture(scope="module")
def low(instances):
    started = []

    def start(name, **env):
        own = instances.start(f"{name}-{secrets.token_hex(3)}", env=env)
        started.append(own)
        return own

    yield start
    for own in started:
        instances.remove(own)


def indicator(page):
    return page.get_by_role("region", name="Captures disk")


def test_every_page_shows_free_and_total_space(page, instance):
    page.goto(instance.base_url)

    expect(indicator(page)).to_contain_text(re.compile(rf"{SIZE} free of {SIZE}"))


def test_low_space_shows_a_warning(page, low):
    own = low("warn-ui", CV_DISK_WARN_BYTES="1P")
    page.goto(own.base_url)

    expect(indicator(page).get_by_role("status")).to_contain_text("Low on space")


def test_below_the_refuse_threshold_the_page_says_why_a_recording_did_not_start(
        page, low, test_pattern_device, unique_label):
    own = low("refuse-ui", CV_DISK_REFUSE_BYTES="1P")
    label = unique_label("refuse")
    own.add_device(test_pattern_device, label=label)
    page.goto(own.base_url)

    expect(indicator(page).get_by_role("status")).to_contain_text("New Recordings and Snapshots are refused")
    page.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(label)).click()
    single = page.get_by_role("region", name="Single View")
    single.get_by_role("button", name="Record", exact=True).click()

    expect(single.locator(".record-control").get_by_role("status")).to_contain_text("refused below")
    expect(single.locator(".badge-recording")).to_have_count(0)
    with own.client() as api:
        api.delete(f"/api/devices/{label}")
