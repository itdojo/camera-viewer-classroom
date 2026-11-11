"""Seam A: only the backend can use the Relay.

The Relay's API, WHEP signalling and RTSP server listen on loopback, and on
Linux the instance shares the host's network, so "loopback" is every local
user and every host-networked container. Being on loopback is therefore not
enough: each needs the credentials the instance generates at every start,
which only the backend holds. Browsers still play through the backend's
session-gated WHEP proxy."""

import json
import time

from harness.whep import plays, watching

# Runs inside the instance's container, i.e. on the Relay's loopback, with no credentials.
PROBE = r'''
import json, socket, sys, urllib.error, urllib.request

camera = sys.argv[1]

def status(request):
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code

def rtsp_status():
    with socket.create_connection(("127.0.0.1", 18554), timeout=10) as conn:
        conn.sendall(("DESCRIBE rtsp://127.0.0.1:18554/" + camera + " RTSP/1.0\r\nCSeq: 1\r\n\r\n").encode())
        return int(conn.recv(4096).split(b" ", 2)[1])

print(json.dumps({
    "api": status("http://127.0.0.1:19997/v3/config/paths/list"),
    "whep": status(urllib.request.Request("http://127.0.0.1:18889/" + camera + "/whep", data=b"v=0\r\n",
                                          method="POST", headers={"Content-Type": "application/sdp"})),
    "rtsp": rtsp_status(),
}))
'''


def test_the_relay_refuses_a_local_client_without_the_backends_credentials(
        instance, test_pattern_device, unique_label):
    label = unique_label("relay-auth")
    camera = f"{label}/test-pattern"
    instance.add_device(test_pattern_device, label=label)

    # The Relay answers 400 for a path it has not loaded yet, and it reloads
    # its configuration only after the add returns; probe until it has.
    deadline = time.monotonic() + 15
    while True:
        result = instance.exec("python", "-c", PROBE, camera)
        assert result.returncode == 0, result.stdout + result.stderr
        statuses = json.loads(result.stdout)
        if 400 not in statuses.values() or time.monotonic() > deadline:
            break
        time.sleep(0.5)

    assert statuses == {"api": 401, "whep": 401, "rtsp": 401}
    with instance.client() as api, watching(api, camera) as feed:
        assert plays(feed), feed.text  # the backend itself still gets through
