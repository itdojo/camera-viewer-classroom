"""The harness's EventStream: leaving it stops its reader before anything else
connects, and a stream that breaks fails ``next()`` at once.

The reader once outlived ``__exit__``. On Linux its blocked TLS read then
shared a reused descriptor with the next connection and corrupted it, which
CI reported as record-layer and handshake errors in whichever request came
next (PR #74). uvicorn serves a tiny SSE app over TLS in a thread, in
process, with a throwaway certificate, so the fast tier needs no Docker.
"""

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from harness.instance import EventStream
from harness.tls import make_certificate, trusting


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    path = scope["path"]
    if path == "/probe":
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})
        return
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
    await send({"type": "http.response.body", "body": b"event: ready\ndata: {}\n\n", "more_body": True})
    if path == "/ended":
        await send({"type": "http.response.body", "body": b""})
        return
    while True:  # /silent sends nothing more; /beating sends a heartbeat, like the instance's EventBus
        await asyncio.sleep(0.05)
        if path == "/beating":
            await send({"type": "http.response.body", "body": b": keepalive\n\n", "more_body": True})


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    cert, key = make_certificate(tmp_path_factory.mktemp("tls"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    running = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, ssl_certfile=str(cert),
                                            ssl_keyfile=str(key), log_level="warning", lifespan="off",
                                            timeout_graceful_shutdown=1))
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not running.started:
        assert time.monotonic() < deadline, "the server did not start"
        time.sleep(0.05)
    yield {"base": f"https://127.0.0.1:{port}", "verify": trusting(cert)}
    running.should_exit = True
    thread.join(10)


def stream(server, path):
    client = httpx.Client(base_url=server["base"], verify=server["verify"], timeout=httpx.Timeout(None, connect=5))
    return EventStream(client, path=path)


@pytest.mark.parametrize("path", ["/silent", "/beating"])
def test_leaving_a_stream_stops_its_reader_and_the_next_connections_are_intact(server, path):
    for _ in range(20):
        with stream(server, path) as events:
            events.next("ready")
        assert not events._thread.is_alive()
        with httpx.Client(base_url=server["base"], verify=server["verify"], timeout=5) as api:
            for _ in range(5):
                assert api.get("/probe").text == "ok"


def test_a_stream_the_server_ends_fails_next_at_once(server):
    with stream(server, "/ended") as events:
        events.next("ready")
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="event stream broke"):
            events.next("devices", timeout=10)

    assert time.monotonic() - started < 5


def test_leaving_a_stream_twice_is_harmless(server):
    events = stream(server, "/silent")
    with events:
        events.next("ready")
    events.__exit__()

    assert not events._thread.is_alive()
