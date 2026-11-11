"""A pinned stream to any port on a Device's own host (#65, before the GPS Sidecar on 9998).

``DeviceClient.stream`` opens the same certificate-pinned HTTPS as the Device API, on a port the
caller names: the fingerprint is checked inside the handshake, before the credentials go out, and
refusals come back as the API's own errors. The host is always the Device's own, so no path can
carry the credentials anywhere else (CODING_STANDARDS rule 2).

The Device here is an in-process HTTPS server on an ephemeral loopback port, with a throwaway
certificate from the host's openssl, so the fast tier needs no Docker.
"""

import asyncio
import base64
import concurrent.futures
import hashlib
import http.server
import socket
import ssl
import threading

import pytest

from camera_viewer.device_client import (
    API_BODY_MAX,
    GPS_SIDECAR_PORT,
    CredentialsRejected,
    DeviceClient,
    DeviceRejected,
    DeviceUnreachable,
    FingerprintMismatch,
)
from harness.tls import make_certificate

USER, PASSWORD = "kit", "s3cret"
BASIC = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
OTHER_FINGERPRINT = "ab" * 32


class Sidecar(http.server.ThreadingHTTPServer):
    """Answers every GET with ``body`` and ``status``, and records each request it read."""

    daemon_threads = True

    def __init__(self, context: ssl.SSLContext, host: str = "127.0.0.1"):
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        super().__init__((host, 0), _Handler)
        self.socket = context.wrap_socket(self.socket, server_side=True)
        self.requests: list[tuple[str, str | None]] = []  # (path, Authorization)
        self.status = 200
        self.body = b""
        self.promised = 0  # bytes beyond ``body`` that Content-Length claims, so the stream breaks

    @property
    def port(self) -> int:
        return self.server_address[1]

    def handle_error(self, request, client_address):
        pass  # a client that stops reading a big answer breaks the pipe; that is the point


class _Handler(http.server.BaseHTTPRequestHandler):
    server: Sidecar

    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get("Authorization")))
        self.send_response(self.server.status)
        self.send_header("Content-Length", str(len(self.server.body) + self.server.promised))
        self.end_headers()
        self.wfile.write(self.server.body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def certificate(tmp_path_factory):
    cert, key = make_certificate(tmp_path_factory.mktemp("sidecar-tls"))
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
    return cert, key, fingerprint


def serve(certificate, host: str = "127.0.0.1") -> Sidecar:
    cert, key, _ = certificate
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = Sidecar(context, host)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def sidecar(certificate):
    server = serve(certificate)
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def ipv6_sidecar(certificate):
    try:
        server = serve(certificate, "::1")
    except OSError:
        pytest.skip("this host has no IPv6 loopback")
    yield server
    server.shutdown()
    server.server_close()


def client(fingerprint: str) -> DeviceClient:
    # The Device's API port is 9997; the stream goes to the port each test names instead.
    return DeviceClient("127.0.0.1", username=USER, password=PASSWORD, fingerprint=fingerprint)


def run(coroutine):
    """Runs ``coroutine`` to completion on a fresh event loop in a worker thread.

    Playwright's sync API (Seam B) leaves an event loop running in the pytest process, and
    ``asyncio.run`` refuses to start inside one, so a plain call fails after any Seam B test.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as worker:
        return worker.submit(asyncio.run, coroutine).result()


async def read_all(device: DeviceClient, port: int, path: str) -> bytes:
    async with device.stream(port, path, read_timeout=5) as chunks:
        return b"".join([chunk async for chunk in chunks])


def test_a_pinned_stream_reaches_another_port_on_the_device_host_with_its_credentials(
        sidecar, certificate):
    sidecar.body = b'{"lat": 51.5}\n{"lat": 51.6}\n'

    body = run(read_all(client(certificate[2]), sidecar.port, "/fixes?interval=30"))

    assert body == b'{"lat": 51.5}\n{"lat": 51.6}\n'
    assert sidecar.requests == [("/fixes?interval=30", BASIC)]


def test_a_different_certificate_is_refused_in_the_handshake_before_any_credential(sidecar):
    with pytest.raises(FingerprintMismatch):
        run(read_all(client(OTHER_FINGERPRINT), sidecar.port, "/fixes"))

    assert sidecar.requests == []


def test_a_port_with_nothing_listening_is_unreachable(sidecar, certificate):
    port = sidecar.port
    sidecar.shutdown()
    sidecar.server_close()

    with pytest.raises(DeviceUnreachable):
        run(read_all(client(certificate[2]), port, "/fixes"))


def test_refused_credentials_are_the_device_apis_own_error(sidecar, certificate):
    sidecar.status = 401

    with pytest.raises(CredentialsRejected):
        run(read_all(client(certificate[2]), sidecar.port, "/fixes"))


def test_a_stream_that_breaks_partway_is_unreachable(sidecar, certificate):
    sidecar.body, sidecar.promised = b'{"lat": 51.5}\n', 1000

    with pytest.raises(DeviceUnreachable):
        run(read_all(client(certificate[2]), sidecar.port, "/fixes"))


@pytest.mark.parametrize("path", ["@evil.example/fixes", "evil.example/fixes", ".evil.example/fixes"])
def test_no_path_can_send_the_credentials_to_another_host(sidecar, certificate, path):
    with pytest.raises(ValueError):
        run(read_all(client(certificate[2]), sidecar.port, path))

    assert sidecar.requests == []


def test_a_refusal_on_another_port_names_that_port_not_the_device_api(sidecar, certificate):
    sidecar.status, sidecar.body = 500, b'{"error": "broken"}'

    with pytest.raises(DeviceRejected) as refused:
        run(read_all(client(certificate[2]), sidecar.port, "/fixes?interval=1"))

    assert str(refused.value) == f"port {sidecar.port} on the Device answered 500 for /fixes: broken"


def test_the_gps_sidecar_port_is_named_the_gps_sidecar(certificate):
    # Nothing listens on 9998 on the test host, so the connection is refused there.
    with pytest.raises(DeviceUnreachable) as unreachable:
        run(read_all(client(certificate[2]), GPS_SIDECAR_PORT, "/v1/fixes"))

    assert str(unreachable.value).startswith("cannot reach the GPS Sidecar at https://127.0.0.1:9998/v1/fixes")


def test_a_refusal_from_the_device_api_keeps_its_wording(sidecar, certificate):
    sidecar.status, sidecar.body = 500, b'{"error": "broken"}'
    api = DeviceClient(f"127.0.0.1:{sidecar.port}", username=USER, password=PASSWORD,
                       fingerprint=certificate[2])

    with pytest.raises(DeviceRejected) as refused:
        run(api.started())

    assert str(refused.value) == "the Device API answered 500 for /v3/info: broken"


def test_an_api_answer_too_big_to_hold_is_refused_at_the_limit(sidecar, certificate):
    sidecar.body = b"[" + b" " * API_BODY_MAX + b"]"
    api = DeviceClient(f"127.0.0.1:{sidecar.port}", username=USER, password=PASSWORD,
                       fingerprint=certificate[2])

    with pytest.raises(DeviceRejected) as refused:
        run(api.clock_skew())

    assert str(refused.value) == f"the Device API sent more than {API_BODY_MAX} bytes for /v3/info"


def test_a_big_refusal_is_read_only_as_far_as_its_error_text(sidecar, certificate):
    # Read whole, this would parse, and its error text would be shown cut to 200 characters.
    sidecar.status, sidecar.body = 500, b'{"error": "' + b"b" * API_BODY_MAX + b'"}'
    api = DeviceClient(f"127.0.0.1:{sidecar.port}", username=USER, password=PASSWORD,
                       fingerprint=certificate[2])

    with pytest.raises(DeviceRejected) as refused:
        run(api.clock_skew())

    assert str(refused.value) == "the Device API answered 500 for /v3/info"  # read to 4 KB: no JSON


@pytest.mark.parametrize("address", ["[::1]:{port}", "[0:0:0:0:0:0:0:1]:{port}"])
def test_a_device_at_an_ipv6_address_answers_its_pinned_api_and_streams(ipv6_sidecar, certificate,
                                                                        address):
    ipv6_sidecar.body = b'{"lat": 51.5}\n'
    device = DeviceClient(address.format(port=ipv6_sidecar.port), username=USER, password=PASSWORD,
                          fingerprint=certificate[2])

    run(device.clock_skew())
    body = run(read_all(device, ipv6_sidecar.port, "/fixes"))

    assert body == b'{"lat": 51.5}\n'
    assert [path for path, _ in ipv6_sidecar.requests] == ["/v3/info", "/fixes"]
