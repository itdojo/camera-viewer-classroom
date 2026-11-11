"""Seam A: Snapshots — a browser uploads a PNG for a Camera, and the instance
names and stores it in the captures folder like any other Capture.

The upload is untrusted. The server decides the file name and where it goes,
stores only a well-formed PNG, and refuses anything too big before it can
fill the disk."""

from __future__ import annotations

import os
import re
import struct
import zlib
from datetime import UTC, datetime, timedelta

import pytest

NAME = re.compile(r"^(?P<stem>.+)-(?P<date>\d{8})-(?P<time>\d{6})-(?P<width>\d+)x(?P<height>\d+)\.png$")


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(width: int, height: int, *, rows: bytes | None = None, ihdr: bytes | None = None) -> bytes:
    """A truecolour 8-bit PNG, every pixel a shade of jade, built by hand so the
    tests do not depend on an imaging library."""
    if rows is None:
        rows = b"".join(b"\x00" + bytes((0, 168, 132)) * width for _ in range(height))
    if ihdr is None:
        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


@pytest.fixture
def camera(instance, test_pattern_device, unique_label):
    """A Camera of its own on the shared instance, removed again afterwards."""
    label = unique_label("snap")
    instance.add_device(test_pattern_device, label=label)
    yield f"{label}/test-pattern"
    with instance.client() as api:
        api.delete(f"/api/devices/{label}")


def upload(api, camera_id, body, **headers):
    return api.post("/api/snapshots", params={"camera": camera_id}, content=body,
                    headers={"Content-Type": "image/png", **headers})


def captures(instance):
    return {p.name for p in instance.captures_dir.iterdir() if not p.name.startswith(".")}


def test_an_uploaded_snapshot_is_stored_under_the_capture_name_for_its_camera(instance, camera):
    image = png(64, 36)
    with instance.client() as api:
        before = datetime.now(UTC)
        response = upload(api, camera, image)
        after = datetime.now(UTC)

    assert response.status_code == 201, response.text
    snapshot = response.json()
    match = NAME.match(snapshot["capture"])
    assert match, snapshot
    device = camera.split("/")[0]
    assert match["stem"] == f"{device}-test-pattern"
    assert (match["width"], match["height"]) == ("64", "36")
    named = datetime.strptime(match["date"] + match["time"], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    assert before - timedelta(seconds=1) <= named <= after  # the shared instance runs in UTC
    assert snapshot["camera"] == camera
    assert (snapshot["width"], snapshot["height"], snapshot["size"]) == (64, 36, len(image))
    assert (instance.captures_dir / snapshot["capture"]).read_bytes() == image


def test_the_client_never_chooses_the_snapshot_file_name_or_where_it_goes(instance, camera):
    with instance.client() as api:
        response = api.post("/api/snapshots", params={"camera": camera, "name": "../../config/evil.png"},
                            content=png(8, 8), headers={
                                "Content-Type": "image/png",
                                "Content-Disposition": 'attachment; filename="../../config/evil.png"'})
        assert response.status_code == 201, response.text
        assert NAME.match(response.json()["capture"])
        assert response.json()["capture"].startswith(f"{camera.split('/')[0]}-test-pattern-")

        for elsewhere in ("../../config/devices", "..%2F..%2Fconfig/devices", camera + "/../../x"):
            assert upload(api, elsewhere, png(8, 8)).status_code == 404
    assert not list(instance.config_dir.rglob("evil*"))
    assert not list(instance.captures_dir.parent.rglob("evil*"))


def corrupt(data: bytes, offset: int) -> bytes:
    return data[:offset] + bytes([data[offset] ^ 0xFF]) + data[offset + 1:]


GOOD = png(16, 9)
IEND = chunk(b"IEND", b"")
NOT_PNGS = {
    "empty": b"",
    "jpeg": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64,
    "html": b"<html><script>alert(1)</script></html>",
    "signature only": GOOD[:8],
    "truncated": GOOD[:-20],
    "bad IHDR checksum": corrupt(GOOD, 29),
    "first chunk not IHDR": GOOD[:8] + chunk(b"tEXt", b"a\x00b") + GOOD[8:],
    "no image data": GOOD[:33] + IEND,
    "no IEND": GOOD[:-len(IEND)],
    "trailing data after IEND": GOOD + b"<?php system($_GET['c']); ?>",
    "zero width": png(0, 9, rows=b"\x00" * 9),
    "bit depth 3": png(16, 9, ihdr=struct.pack(">IIBBBBB", 16, 9, 3, 2, 0, 0, 0)),
    "interlace method 7": png(16, 9, ihdr=struct.pack(">IIBBBBB", 16, 9, 8, 2, 0, 0, 7)),
    "image data not zlib": GOOD[:33] + chunk(b"IDAT", b"not deflate at all") + IEND,
    "image data a row short": png(16, 9, rows=b"".join(b"\x00" + b"\x10" * 48 for _ in range(8))),
    "image data a row long": png(16, 9, rows=b"".join(b"\x00" + b"\x10" * 48 for _ in range(10))),
    "unknown row filter": png(16, 9, rows=b"".join(b"\x09" + b"\x10" * 48 for _ in range(9))),
    "unknown critical chunk": GOOD[:-len(IEND)] + chunk(b"ZZZZ", b"x") + IEND,
    "inflates far past its header": png(16, 9, rows=bytes(256 << 20)),  # a 256 KB zip bomb
    "more pixels than any Feed": png(8192, 8192, rows=b""),
}


@pytest.mark.parametrize("body", NOT_PNGS.values(), ids=NOT_PNGS.keys())
def test_an_upload_that_is_not_a_well_formed_png_is_refused_and_nothing_is_stored(instance, camera, body):
    before = captures(instance)
    with instance.client() as api:
        response = upload(api, camera, body)

    assert response.status_code == 422, response.text
    assert response.json()["error"] == "invalid_png"
    assert captures(instance) == before


def test_an_upload_that_does_not_say_it_is_a_png_is_refused(instance, camera):
    before = captures(instance)
    with instance.client() as api:
        response = upload(api, camera, png(8, 8), **{"Content-Type": "text/html"})

    assert response.status_code == 415, response.text
    assert response.json()["error"] == "unsupported_media_type"
    assert captures(instance) == before


def test_a_snapshot_bigger_than_the_limit_is_refused_however_it_is_sent(
        instances, test_pattern_device, unique_label):
    own = instances.start(unique_label("snap-limit"), env={"CV_SNAPSHOT_MAX_BYTES": "4096"})
    label = unique_label("snap")
    own.add_device(test_pattern_device, label=label)
    camera = f"{label}/test-pattern"
    # Noise does not compress, so this PNG is about 12 KB.
    noise = png(64, 64, rows=b"".join(b"\x00" + os.urandom(192) for _ in range(64)))
    assert len(noise) > 4096

    def chunked():  # no Content-Length: the server has to count as it reads
        for start in range(0, len(noise), 1000):
            yield noise[start:start + 1000]

    with own.client() as api:
        declared = upload(api, camera, noise)
        streamed = upload(api, camera, chunked())
        small = upload(api, camera, png(8, 8))

    for response in (declared, streamed):
        assert response.status_code == 413, response.text
        assert response.json()["error"] == "snapshot_too_large"
        assert response.json()["max_bytes"] == 4096
    assert small.status_code == 201, small.text
    assert captures(own) == {small.json()["capture"]}
