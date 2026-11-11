"""Seam B: a timed Recording. The student picks a preset duration before
recording, every view counts down the time it has left, and the Recording
ends on its own when the time is up."""

import re
import secrets

import pytest
from playwright.sync_api import expect

LEFT = re.compile(r"^(\d+):(\d\d) left$")


@pytest.fixture(scope="module")
def timed(instances):
    own = instances.start(f"timed-ui-{secrets.token_hex(3)}", env={"CV_RECORDING_PRESETS": "12,60,300"})
    yield own
    instances.remove(own)


def seconds_left(locator):
    # text_content, not inner_text: the badge is uppercased by CSS.
    minutes, seconds = LEFT.match(locator.text_content().strip()).groups()
    return int(minutes) * 60 + int(seconds)


def test_a_timed_recording_counts_down_in_every_view_and_ends_on_its_own(
        page, timed, test_pattern_device, unique_label):
    label = unique_label("timed")
    timed.add_device(test_pattern_device, label=label)
    page.goto(timed.base_url)
    cameras = page.get_by_role("list", name="Cameras")
    cameras.get_by_role("button", name=re.compile(label)).click()
    single = page.get_by_role("region", name="Single View")

    duration = single.get_by_role("combobox", name="Duration")
    expect(duration.locator("option")).to_have_text(["Until stopped", "12 s", "1 min", "5 min"])
    duration.select_option(label="12 s")
    single.get_by_role("button", name="Record", exact=True).click()

    countdown = single.get_by_text(LEFT)
    expect(countdown).to_be_visible(timeout=5_000)
    expect(cameras.locator("li").filter(has_text=label).get_by_text(LEFT)).to_be_visible()
    first = seconds_left(countdown)
    assert 9 <= first <= 12
    page.wait_for_timeout(2_500)
    assert first - 4 <= seconds_left(countdown) <= first - 1  # it ticks without any server event
    expect(single.get_by_role("combobox", name="Duration")).to_be_hidden()  # no preset while recording

    expect(single.get_by_role("button", name="Record", exact=True)).to_be_visible(timeout=15_000)  # ended by its timer
    expect(single.locator(".badge")).to_have_count(0)
    expect(single.get_by_role("combobox", name="Duration")).to_be_visible()
    with timed.client() as api:
        assert all(not r["camera"].startswith(label) for r in api.get("/api/recordings").json())
        api.delete(f"/api/devices/{label}")
    assert any(p.name.startswith(f"{label}-test-pattern-") for p in timed.captures_dir.iterdir())
