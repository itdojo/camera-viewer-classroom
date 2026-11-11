"""Seam B: a Feed that freezes in one browser while the Relay still reports it
Live shows a Stall overlay there, and only there.

The freeze is made inside the browser, on its receive path. An init script
puts an RTCRtpScriptTransform (WebRTC encoded transform) on every receiver
the page creates; while ``window.mediaPath.block()`` is in force it
drops every encoded frame between the browser's RTP receiver and its decoder.
Packets keep arriving and ICE stays connected, so the Relay keeps serving the
Feed and keeps reporting it Live, but nothing reaches the decoder and the
video's decoded-frame count stops: what a lossy Wi-Fi leg or a starved
browser looks like from the page. ``unblock()`` lets frames through again from
the next keyframe (the test pattern sends one a second), as a decoder would
recover after loss.
"""

import re
import time

from playwright.sync_api import expect

BLOCKABLE_MEDIA_PATH = """(() => {
    const worker = new Worker(URL.createObjectURL(new Blob([`
        let blocked = false, awaitingKeyframe = false;
        onmessage = ({ data }) => {
            blocked = data.blocked;
            if (!blocked) awaitingKeyframe = true;
        };
        onrtctransform = ({ transformer }) => {
            transformer.readable.pipeThrough(new TransformStream({
                transform(frame, controller) {
                    if (blocked) return;
                    if (awaitingKeyframe && frame.type !== 'key') return;
                    awaitingKeyframe = false;
                    controller.enqueue(frame);
                },
            })).pipeTo(transformer.writable);
        };
    `], { type: 'text/javascript' })));
    const Native = window.RTCPeerConnection;
    // Set on the receiver as soon as it exists: Chromium 153 never runs a
    // transform set later, in the 'track' event, for this WHEP flow.
    window.RTCPeerConnection = class extends Native {
        addTransceiver(...args) {
            const transceiver = super.addTransceiver(...args);
            transceiver.receiver.transform = new RTCRtpScriptTransform(worker, {});
            return transceiver;
        }
    };
    window.mediaPath = {
        block: () => worker.postMessage({ blocked: true }),
        unblock: () => worker.postMessage({ blocked: false }),
    };
})();"""

TILE_PLAYING = """(label) => {
    const tile = [...document.querySelectorAll('[aria-label="Multi View"] article')]
        .find((t) => t.getAttribute('aria-label').includes(label));
    const video = tile?.querySelector('video');
    return Boolean(video && video.videoWidth > 0 && video.currentTime > 1);
}"""


def watch_on_a_tile(page, instance, label):
    page.add_init_script(BLOCKABLE_MEDIA_PATH)
    page.goto(instance.base_url)
    page.get_by_role("list", name="Cameras").get_by_role(
        "checkbox", name=re.compile(rf"\b{label}\b")).check()
    tile = page.get_by_role("region", name="Multi View").get_by_role("article", name=re.compile(label))
    page.wait_for_function(TILE_PLAYING, arg=label, timeout=20_000)
    expect(tile.get_by_text("Live", exact=True)).to_be_visible(timeout=30_000)
    return tile


def test_a_tile_frozen_in_this_browser_shows_the_stall_overlay_until_frames_resume(
        page, instance, test_pattern_device, unique_label):
    label = unique_label("stall")
    instance.add_device(test_pattern_device, label=label)
    tile = watch_on_a_tile(page, instance, label)
    stall = tile.get_by_text("Stalled", exact=True)
    expect(stall).to_be_hidden()

    page.evaluate("mediaPath.block()")
    blocked_at = time.monotonic()
    expect(stall).to_be_visible(timeout=5_000)

    assert 1.5 <= time.monotonic() - blocked_at <= 3.5  # "about 2 seconds"
    expect(tile.get_by_text("Live", exact=True)).to_be_visible()  # the Relay still says Live

    page.evaluate("mediaPath.unblock()")
    expect(stall).to_be_hidden(timeout=5_000)


def test_the_stall_threshold_is_the_instances_feed_stall_seconds(
        page, instances, test_pattern_device, unique_label):
    own = instances.start(unique_label("stall-tuned"), env={"FEED_STALL_SECONDS": "5"})
    label = unique_label("tuned")
    own.add_device(test_pattern_device, label=label)
    tile = watch_on_a_tile(page, own, label)
    stall = tile.get_by_text("Stalled", exact=True)

    page.evaluate("mediaPath.block()")
    blocked_at = time.monotonic()
    time.sleep(3.5)  # past the 2 s default
    expect(stall).to_be_hidden()
    expect(stall).to_be_visible(timeout=5_000)

    assert 4.5 <= time.monotonic() - blocked_at <= 6.5


SINGLE_VIEW_PLAYING = """() => {
    const video = document.querySelector('[aria-label="Single View"] video');
    return video && video.videoWidth > 0 && video.currentTime > 1;
}"""


def test_a_stall_in_single_view_changes_no_feed_status_any_browser_sees(
        browser, instance, test_pattern_device, unique_label):
    label = unique_label("stall-single")
    instance.add_device(test_pattern_device, label=label)
    watcher = browser.new_context().new_page()
    watcher_tile = watch_on_a_tile(watcher, instance, label)
    viewer = browser.new_context().new_page()
    viewer.add_init_script(BLOCKABLE_MEDIA_PATH)
    viewer.goto(instance.base_url)
    viewer.get_by_role("list", name="Cameras").get_by_role("button", name=re.compile(label)).click()
    viewer.wait_for_function(SINGLE_VIEW_PLAYING, timeout=20_000)
    single_view = viewer.get_by_role("region", name="Single View")

    viewer.evaluate("mediaPath.block()")
    expect(single_view.get_by_text("Stalled", exact=True)).to_be_visible(timeout=5_000)
    time.sleep(2)  # well past the threshold, and two Feed Monitor samples

    expect(single_view.locator("header").get_by_text("Live", exact=True)).to_be_visible()
    expect(watcher_tile.get_by_text("Live", exact=True)).to_be_visible()
    expect(watcher_tile.get_by_text("Stalled", exact=True)).to_be_hidden()
    with instance.client() as client:
        cameras = {c["id"]: c for c in client.get("/api/cameras").json()}
    assert cameras[f"{label}/test-pattern"]["feed_status"] == "live"

    viewer.evaluate("mediaPath.unblock()")
    expect(single_view.get_by_text("Stalled", exact=True)).to_be_hidden(timeout=5_000)
    for page in (watcher, viewer):
        page.goto("about:blank")  # pagehide ends the WHEP session (see tests/conftest.py)
        page.context.close()
