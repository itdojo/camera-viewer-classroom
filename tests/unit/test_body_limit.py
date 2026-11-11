"""No request body over the cap reaches a route, however it is sent (``camera_viewer.body_limit``).

The app behind the gate is a stub that records each body it is handed, so a refused request is
seen never to reach it. The fast tier needs no Docker.
"""

import asyncio
import concurrent.futures

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from camera_viewer.body_limit import BodyLimit

LIMIT = 1024


@pytest.fixture
def seen():
    return []


@pytest.fixture
def post(seen):
    async def echo(request: Request) -> JSONResponse:
        body = await request.body()
        seen.append(body)
        return JSONResponse({"size": len(body)})

    app = BodyLimit(Starlette(routes=[Route("/api/echo", echo, methods=["POST"]),
                                      Route("/api/snapshots", echo, methods=["POST"])]),
                    max_bytes=LIMIT)

    async def send(path, content):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://instance") as client:
            return await client.post(path, content=content)

    def call(path, content):
        # A fresh loop in a worker thread: Seam B's Playwright leaves one running (test_pinned_stream).
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as worker:
            return worker.submit(asyncio.run, send(path, content)).result()

    return call


async def chunks(data: bytes):  # no Content-Length: the gate has to count as it reads
    for start in range(0, len(data), 100):
        yield data[start:start + 100]


def test_a_body_within_the_limit_reaches_the_route_whole(post, seen):
    response = post("/api/echo", b"x" * LIMIT)

    assert response.status_code == 200
    assert seen == [b"x" * LIMIT]


def test_a_chunked_body_within_the_limit_reaches_the_route_whole(post, seen):
    response = post("/api/echo", chunks(b"y" * 900))

    assert response.status_code == 200
    assert seen == [b"y" * 900]


@pytest.mark.parametrize("send", [lambda body: body, chunks], ids=["declared", "chunked"])
def test_a_body_over_the_limit_is_refused_before_the_route_runs(post, seen, send):
    response = post("/api/echo", send(b"z" * (LIMIT + 1)))

    assert response.status_code == 413
    assert response.json() == {"error": "body_too_large",
                               "message": f"a request body may be at most {LIMIT} bytes",
                               "max_bytes": LIMIT}
    assert seen == []


def test_the_snapshot_upload_keeps_its_own_limit(post, seen):
    response = post("/api/snapshots", b"p" * (LIMIT * 4))

    assert response.status_code == 200
    assert seen == [b"p" * (LIMIT * 4)]
