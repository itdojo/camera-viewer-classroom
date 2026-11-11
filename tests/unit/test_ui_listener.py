"""The UI listener's one port (ADR 0014): a TLS client is served over HTTPS
with its own address, and anything else is redirected to https:// or closed.

uvicorn runs the listener's protocol in a thread, in process, around a tiny
ASGI app that echoes the scheme and client address; the certificate is a
throwaway from the host's openssl, so the fast tier needs no Docker.
"""

import logging
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from camera_viewer.listener import HttpsOrRedirect, https_protocol, server_context
from harness.tls import make_certificate, trusting


async def echo(scope, receive, send):
    if scope["type"] != "http":
        return
    body = f"{scope['scheme']} {scope['client'][0]} {scope['path']}".encode()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": body})


@pytest.fixture(scope="module")
def listener(tmp_path_factory):
    cert, key = make_certificate(tmp_path_factory.mktemp("tls"))
    protocol = https_protocol(server_context(str(cert), str(key)))
    protocol.first_request_seconds = 1.0
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(echo, host="127.0.0.1", port=port, http=protocol,
                                           log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "the listener did not start"
        time.sleep(0.05)
    yield {"port": port, "cert": cert}
    server.should_exit = True
    thread.join(10)


def raw(port, data, *, read=True):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(data)
        if not read:
            return b""
        chunks = []
        try:
            while chunk := sock.recv(4096):
                chunks.append(chunk)
        except ConnectionResetError:
            pass  # closed with bytes still unread: no answer either
        return b"".join(chunks)


def test_a_tls_client_is_served_over_https_with_its_own_address(listener):
    with httpx.Client(verify=trusting(listener["cert"]), timeout=5) as client:
        first = client.get(f"https://127.0.0.1:{listener['port']}/a")
        second = client.get(f"https://127.0.0.1:{listener['port']}/b")  # same connection, kept alive

    assert first.text == "https 127.0.0.1 /a"
    assert second.text == "https 127.0.0.1 /b"


def test_plain_http_is_redirected_to_the_same_host_and_target(listener):
    answer = raw(listener["port"], b"GET /live?camera=1 HTTP/1.1\r\nHost: laptop.lab:8080\r\n\r\n")

    head = answer.decode("latin-1")
    assert head.startswith("HTTP/1.1 301 ")
    assert "\r\nLocation: https://laptop.lab:8080/live?camera=1\r\n" in head
    assert "\r\nConnection: close\r\n" in head


@pytest.mark.parametrize("host", ["", "Host: a b\r\n", "Host: evil.example/path\r\n"],
                         ids=["no-host", "host-with-a-space", "host-with-a-path"])
def test_a_missing_or_strange_host_redirects_to_the_listeners_own_address(listener, host):
    answer = raw(listener["port"], f"GET / HTTP/1.1\r\n{host}\r\n".encode())

    assert f"Location: https://127.0.0.1:{listener['port']}/\r\n".encode() in answer


def test_a_request_line_that_is_not_http_is_closed_without_an_answer(listener):
    assert raw(listener["port"], b"HELLO\r\n\r\n") == b""


def test_a_broken_handshake_is_closed_logged_and_the_listener_carries_on(listener, caplog):
    with caplog.at_level(logging.INFO, logger="camera_viewer.listener"):
        assert raw(listener["port"], b"\x16\x03\x01junk-that-is-no-client-hello") == b""
        # asyncio closes the connection itself before the listener's coroutine logs the failure,
        # so the client can see the close first (CI run 39090827659).
        deadline = time.monotonic() + 5
        while not any("TLS handshake failed" in message for message in caplog.messages):
            assert time.monotonic() < deadline, f"the failure was never logged: {caplog.messages}"
            time.sleep(0.02)

    with httpx.Client(verify=trusting(listener["cert"]), timeout=5) as client:
        assert client.get(f"https://127.0.0.1:{listener['port']}/").status_code == 200


def test_a_connection_that_sends_nothing_is_closed_after_the_first_request_timeout(listener, caplog):
    with caplog.at_level(logging.INFO, logger="camera_viewer.listener"), \
            socket.create_connection(("127.0.0.1", listener["port"]), timeout=5) as sock:
        started = time.monotonic()
        assert sock.recv(1) == b""
        waited = time.monotonic() - started

    assert any("nothing usable within 1s" in message for message in caplog.messages)

    assert HttpsOrRedirect.first_request_seconds == 10.0  # the shipped value; the fixture shortens it
    assert 0.5 < waited < 4


def test_an_endless_plain_request_head_is_cut_off(listener):
    answer = raw(listener["port"], b"GET / HTTP/1.1\r\nX: " + b"a" * 9000)

    assert answer == b""


def test_a_plain_client_that_gives_up_leaves_the_listener_working(listener):
    raw(listener["port"], b"", read=False)

    with httpx.Client(verify=trusting(listener["cert"]), timeout=5) as client:
        assert client.get(f"https://127.0.0.1:{listener['port']}/").text.startswith("https ")
