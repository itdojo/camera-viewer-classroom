"""Seam B: the macOS deployment path. Every instance under test runs
compose.yaml plus compose.macos.yaml, so any Feed that plays has played
through it. Chromium on the same host connects even without the advertised
address (the Relay also offers its interface addresses, loopback included),
so this checks separately that the Relay offers browsers CV_HOST_IP on the
published WebRTC port, which is what a browser on another laptop depends on.
The address is from TEST-NET-1, so it can only come from CV_HOST_IP."""

import re

from test_single_view import PLAYING, open_camera

HOST_IP = "192.0.2.10"


def test_a_feed_plays_and_the_relay_advertises_the_host_ip_on_the_published_port(
        page, instances, test_pattern_device):
    mac = instances.start("macos-host-ip", host_ip=HOST_IP)
    mac.add_device(test_pattern_device, label="tp-mac")

    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/whep")) as whep:
        open_camera(page, mac, "tp-mac")
    page.wait_for_function(PLAYING, timeout=20_000)

    candidates = re.findall(r"^a=candidate:.*$", whep.value.text(), flags=re.MULTILINE)
    port = mac.webrtc_udp_port
    assert any(re.search(rf" udp \d+ {re.escape(HOST_IP)} {port} typ host", c, re.I)
               for c in candidates), candidates
