"""A real GNSS receiver read by the GPS Sidecar on a Device built from the kit.

Opt-in, because it needs hardware: skipped unless ``CV_HW_GPS_ADDRESSES``
names the Device, one or more addresses separated by commas. The Device runs
any kit variant with the receiver plugged in, started as a kit builder
starts it, from the variant's folder::

    ../gen-cert.sh                       # records GPS_TTY in .env
    docker compose --profile gps up -d

Then, with the password and fingerprint ``gen-cert.sh`` printed::

    CV_HW_GPS_ADDRESSES=192.0.2.94 \\
    CV_HW_GPS_PASSWORD=<password> CV_HW_GPS_FINGERPRINT=<fingerprint> \\
    uv run pytest tests/device_kit/test_gps_hardware.py -s

``CV_HW_GPS_USER`` defaults to ``viewer`` and ``CV_HW_GPS_PORT`` to 9998. A
cold receiver can take minutes to get its first fix and needs a view of the
sky; ``CV_HW_GPS_FIX_SECONDS`` (default 180) is how long to wait for one.
Stop the Sidecar afterwards with ``docker compose --profile gps down``; a
plain ``down`` leaves it running.
"""

import base64
import hashlib
import http.client
import json
import os
import ssl
import time

import pytest

ADDRESSES = [a.strip() for a in os.environ.get("CV_HW_GPS_ADDRESSES", "").split(",") if a.strip()]
pytestmark = pytest.mark.skipif(not ADDRESSES, reason="no GPS Device: set CV_HW_GPS_ADDRESSES")

PORT = int(os.environ.get("CV_HW_GPS_PORT", "9998"))
FIX_SECONDS = float(os.environ.get("CV_HW_GPS_FIX_SECONDS", "180"))


class PinnedConnection(http.client.HTTPSConnection):
    """HTTPS to the Sidecar, refused unless the Device presents the pinned
    certificate, checked before any credentials are sent."""

    def connect(self):
        super().connect()
        presented = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if presented != os.environ["CV_HW_GPS_FINGERPRINT"].lower():
            self.close()
            raise ssl.SSLError(f"the Sidecar presented {presented}, not the pinned certificate")


def stream_lines(address):
    """Each line of the Sidecar's stream at 1 s intervals, as parsed JSON."""
    unverified = ssl.create_default_context()
    unverified.check_hostname = False
    unverified.verify_mode = ssl.CERT_NONE  # trust comes from the pin, as in the instance
    connection = PinnedConnection(address, PORT, context=unverified, timeout=30)
    connection.connect()
    user = os.environ.get("CV_HW_GPS_USER", "viewer")
    token = f"{user}:{os.environ['CV_HW_GPS_PASSWORD']}".encode()
    connection.request("GET", "/v1/fixes?interval=1",
                       headers={"Authorization": "Basic " + base64.b64encode(token).decode()})
    response = connection.getresponse()
    assert response.status == 200, response.read()
    try:
        while line := response.readline():
            yield json.loads(line)
    finally:
        connection.close()


@pytest.mark.parametrize("address", ADDRESSES)
def test_a_fix_from_the_real_receiver_arrives(address):
    deadline = time.monotonic() + FIX_SECONDS
    first = None
    for fix in stream_lines(address):
        first = first or fix
        if fix["mode"] in ("2d", "3d") and fix["lat"] is not None and fix["lon"] is not None:
            print(f"\n{address}: {json.dumps(fix)}")
            assert -90 <= fix["lat"] <= 90 and -180 <= fix["lon"] <= 180
            return
        assert time.monotonic() < deadline, f"no fix within {FIX_SECONDS:g} s; the receiver said {first}"
    pytest.fail("the stream ended")
