"""Snapshots: what the instance accepts as an uploaded frame.

A browser draws the playing video onto a canvas at its intrinsic size and
uploads the PNG. The upload is untrusted, so ``read_png`` checks that it is
one well-formed PNG before anything is written: the signature, every chunk's
length and CRC, IHDR first with a valid header, the image data inflating to
exactly the rows the header promises with a valid filter on every row, IEND
last and nothing after it. It returns the PNG's size; the Capture Store names
the file from the Camera, the server's clock and that size, never from
anything the client sent.

Inflating is bounded by the header (``MAX_SIDE``, ``MAX_PIXELS``) and done
in pieces, so a small upload that inflates to gigabytes is refused once it
passes the size its header declares, without holding the image in memory.
Interlaced PNGs are refused: a canvas never makes one.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_SIDE = 8192                 # pixels, either side
MAX_PIXELS = 7680 * 4320        # 8K UHD; a v1 Feed is 1080p or below
INFLATE_PIECE = 1 << 20         # inflate at most this many bytes at a time

# Valid bit depths for each PNG colour type, and its samples per pixel.
BIT_DEPTHS = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
FILTERS = range(5)              # None, Sub, Up, Average, Paeth


class InvalidPng(ValueError):
    """The upload is not a PNG the instance will store."""


@dataclass(frozen=True)
class PngInfo:
    width: int
    height: int


def _chunks(data: bytes):
    """Yield ``(type, data)`` for every chunk, checking lengths and CRCs."""
    offset = len(SIGNATURE)
    while offset < len(data):
        if offset + 12 > len(data):
            raise InvalidPng("truncated chunk")
        length, kind = struct.unpack(">I4s", data[offset:offset + 8])
        end = offset + 8 + length
        if length > 0x7FFFFFFF or end + 4 > len(data):
            raise InvalidPng("truncated chunk")
        if not kind.isalpha() or not kind.isascii():
            raise InvalidPng("malformed chunk type")
        body = data[offset + 8:end]
        (crc,) = struct.unpack(">I", data[end:end + 4])
        if zlib.crc32(kind + body) != crc:
            raise InvalidPng(f"bad checksum in {kind.decode()} chunk")
        yield kind, body
        offset = end + 4


def _header(body: bytes) -> tuple[int, int, int]:
    """Check IHDR and return ``(width, height, bytes per row without the filter byte)``."""
    if len(body) != 13:
        raise InvalidPng("malformed IHDR")
    width, height, depth, colour, compression, filtering, interlace = struct.unpack(">IIBBBBB", body)
    if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE) or width * height > MAX_PIXELS:
        raise InvalidPng(f"{width}x{height} is not a size the instance stores "
                         f"(each side 1 to {MAX_SIDE}, at most {MAX_PIXELS} pixels)")
    if depth not in BIT_DEPTHS.get(colour, ()):
        raise InvalidPng(f"bit depth {depth} is not valid for colour type {colour}")
    if compression != 0 or filtering != 0:
        raise InvalidPng("unknown compression or filter method")
    if interlace != 0:
        raise InvalidPng("interlaced PNGs are not accepted")
    return width, height, (width * CHANNELS[colour] * depth + 7) // 8


def _check_image_data(compressed: bytes, height: int, row: int) -> None:
    """Inflate in pieces, never past what the header promises, and check every
    row's filter byte."""
    stride = row + 1
    expected = stride * height
    inflater = zlib.decompressobj()
    seen = 0
    pending = compressed
    try:
        while True:
            piece = inflater.decompress(pending, INFLATE_PIECE)
            pending = inflater.unconsumed_tail
            if seen + len(piece) > expected:
                raise InvalidPng("image data is longer than the header says")
            # The filter byte of each row that starts inside this piece.
            first = -seen % stride
            if any(b not in FILTERS for b in piece[first::stride]):
                raise InvalidPng("unknown row filter")
            seen += len(piece)
            if inflater.eof or (not piece and not pending):
                break
    except zlib.error as exc:
        raise InvalidPng(f"image data does not inflate: {exc}") from exc
    if not inflater.eof:
        raise InvalidPng("image data is truncated")
    if seen != expected:
        raise InvalidPng("image data is shorter than the header says")
    if inflater.unused_data:
        raise InvalidPng("trailing bytes after the image data")


def read_png(data: bytes) -> PngInfo:
    """Return the size of ``data`` if it is one well-formed, non-interlaced
    PNG; raise ``InvalidPng`` otherwise."""
    if not data.startswith(SIGNATURE):
        raise InvalidPng("not a PNG: the signature is missing")
    header = None
    palette = False
    idat: list[bytes] = []
    idat_closed = False
    ended = False
    for kind, body in _chunks(data):
        if ended:
            raise InvalidPng("chunks after IEND")
        if header is None:
            if kind != b"IHDR":
                raise InvalidPng("the first chunk is not IHDR")
            header = _header(body)
            colour = body[9]
            continue
        if kind == b"IHDR":
            raise InvalidPng("more than one IHDR")
        if kind == b"PLTE":
            if idat or palette or len(body) % 3 or not 0 < len(body) <= 768:
                raise InvalidPng("misplaced or malformed PLTE")
            palette = True
        elif kind == b"IDAT":
            if idat_closed:
                raise InvalidPng("IDAT chunks are not consecutive")
            idat.append(body)
        elif kind == b"IEND":
            if body:
                raise InvalidPng("IEND carries data")
            ended = True
        elif kind[0] & 0x20 == 0:  # an upper-case first letter: critical, and unknown
            raise InvalidPng(f"unknown critical chunk {kind.decode()}")
        if idat and kind != b"IDAT":
            idat_closed = True
    if header is None:
        raise InvalidPng("no IHDR")
    if not ended:
        raise InvalidPng("no IEND: the PNG is truncated")
    if not idat:
        raise InvalidPng("no image data")
    if colour == 3 and not palette:
        raise InvalidPng("a palette image without PLTE")
    width, height, row = header
    _check_image_data(b"".join(idat), height, row)
    return PngInfo(width, height)
