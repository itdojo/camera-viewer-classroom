"""Instances with a Map Extract in their config folder (#61).

``FIXTURE`` is the committed test extract, a 326 KB cut of the Protomaps planet build around
Westminster, London: bbox -0.1270,51.4985,-0.1200,51.5030 (west, south, east, north), zooms 14
and 15, cut on 2026-10-15 from ``https://build.protomaps.com/20261015.pmtiles`` (Protomaps
basemap tiles 4.15.2, OSM data replicated 2026-10-15T04:00Z) with go-pmtiles 1.31.2, the
official release binary for darwin arm64 (asset sha256 40528f7f…1880f3d, matching the digest
GitHub lists for it)::

    pmtiles extract https://build.protomaps.com/20261015.pmtiles westminster.pmtiles \\
        --bbox=-0.1270,51.4985,-0.1200,51.5030 --minzoom=14 --maxzoom=15

The data is © OpenStreetMap contributors under the ODbL. It holds both banks of the Thames, so a
map drawn from it shows water and land side by side.

``colour_share`` measures how much of a screenshot is one colour, which is how Seam B tells
which basemap is on screen. ``solid_png`` makes the stand-in tile for an online tile server (#62), a one-colour PNG a test
answers tile requests with, so it can tell the online basemap on screen by its colour.
"""

from __future__ import annotations

import base64
import os
import shutil
import struct
import zlib
from collections.abc import Mapping
from pathlib import Path

from playwright.sync_api import Page

from harness.instance import Instance, InstancePool

FIXTURE = Path(__file__).parents[1] / "fixtures" / "westminster.pmtiles"
# A point inside the fixture, by the river.
LATITUDE, LONGITUDE = 51.50000, -0.12346


def start_with_extracts(instances: InstancePool, name: str,
                        extracts: Mapping[str, tuple[Path, float]], *,
                        env: Mapping[str, str] | None = None) -> Instance:
    """Start an instance whose ``config/maps`` folder holds ``extracts``, given as
    ``{file name: (source file, modification time)}``, with ``env`` as its settings."""
    maps = instances.workdir / name / "config" / "maps"
    maps.mkdir(parents=True)
    for file_name, (source, mtime) in extracts.items():
        shutil.copyfile(source, maps / file_name)
        os.utime(maps / file_name, (mtime, mtime))
    return instances.start(name, env=env)


def solid_png(rgb: tuple[int, int, int], size: int = 256) -> bytes:
    """A ``size``-pixel square PNG of one colour, written with the standard library."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    row = b"\0" + bytes(rgb) * size  # filter type 0, then the row's pixels
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(row * size))
            + chunk(b"IEND", b""))


# The share of a PNG's pixels within a few levels of an RGB colour, counted in the page, which
# can decode an image with no Python imaging library.
COLOUR_SHARE = """async ([png, rgb]) => {
    const image = new Image();
    image.src = `data:image/png;base64,${png}`;
    await image.decode();
    const canvas = new OffscreenCanvas(image.width, image.height);
    const context = canvas.getContext('2d');
    context.drawImage(image, 0, 0);
    const { data } = context.getImageData(0, 0, image.width, image.height);
    let near = 0;
    for (let i = 0; i < data.length; i += 4) {
        if (Math.abs(data[i] - rgb[0]) <= 6 && Math.abs(data[i + 1] - rgb[1]) <= 6
            && Math.abs(data[i + 2] - rgb[2]) <= 6) near += 1;
    }
    return near / (data.length / 4);
}"""


def colour_share(page: Page, png: bytes, rgb: tuple[int, int, int]) -> float:
    """The share of ``png``'s pixels within a few levels of ``rgb``, counted in ``page``."""
    return page.evaluate(COLOUR_SHARE, [base64.b64encode(png).decode(), list(rgb)])
