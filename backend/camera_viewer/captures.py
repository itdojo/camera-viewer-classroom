"""Capture Store: owns the captures folder and the name every Capture gets.

Every Capture, Recording or Snapshot, is named

    <device>-<camera>-<yyyymmdd>-<hhmmss>-<width>x<height>[-part-n].<ext>

with the Device label, the Camera name in kebab-case, the time in the
instance's local time zone (``TZ``), and the Feed's received resolution.
``CaptureName`` builds it; ``CaptureStore.place`` moves a finished file into
the folder under it without ever overwriting another Capture.

The Relay writes Recording segments under ``.recording/`` inside the captures
folder, so moving a finished one into place is a rename on one filesystem.
A Snapshot upload is written under ``.incoming/`` first and placed the same
way, so a half-written file never shows as a Capture. Dot-directories are the
store's own working space, never Captures.

The Captures list is what the folder holds right now: every regular file
whose name could be a Capture name (kebab-case, ``.mp4`` or ``.png``),
newest first. Only those can be served or deleted, so a name from a client
never reaches a dot-entry, a subfolder, a symlink or anything outside the
folder. Listeners (``subscribe``) hear of every change the instance makes;
a file added or removed by hand on the host shows with the next one.
"""

from __future__ import annotations

import os
import re
import stat
import struct
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

STAGING = ".recording"
INCOMING = ".incoming"

# What a Capture's file name can look like. Nothing else in the folder is a Capture.
CAPTURE_FILE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.(mp4|png)")
KINDS = {"mp4": "recording", "png": "snapshot"}


def kebab(text: str) -> str:
    """``USB_Cam 2`` -> ``usb-cam-2``: lowercase letters and digits, joined by single hyphens."""
    return "-".join(re.findall(r"[a-z0-9]+", text.lower())) or "camera"


@dataclass(frozen=True)
class CaptureName:
    """What a Capture's name says: whose Feed, when, at what size."""

    device: str
    camera: str
    when: datetime        # timezone-aware; rendered in the instance's local time
    width: int
    height: int
    ext: str              # "mp4" or "png", without the dot
    part: int | None = None  # 2, 3, ... for a Recording continued after a Feed drop

    @property
    def stem(self) -> str:
        """The name up to the resolution, shared by every part of one Recording."""
        local = self.when.astimezone()
        return f"{self.device}-{kebab(self.camera)}-{local:%Y%m%d}-{local:%H%M%S}"

    def render(self, part: int | None = None) -> str:
        part = self.part if part is None else part
        suffix = f"-part-{part}" if part and part > 1 else ""
        return f"{self.stem}-{self.width}x{self.height}{suffix}.{self.ext}"


def mp4_video_size(path: Path) -> tuple[int, int] | None:
    """The width and height of the first video track of an MP4 file, read
    from its ``moov/trak/tkhd`` boxes, or None if it has none or can't be
    read. A fragmented MP4 (what the Relay writes) has its ``moov`` at the
    start, so only the head of the file is read."""
    try:
        with open(path, "rb") as file:
            data = file.read(1 << 20)
    except OSError:
        return None

    def boxes(start: int, end: int):
        at = start
        while at + 8 <= end:
            size, kind = struct.unpack(">I4s", data[at:at + 8])
            header = 8
            if size == 1 and at + 16 <= end:
                size, header = struct.unpack(">Q", data[at + 8:at + 16])[0], 16
            elif size == 0:
                size = end - at
            if size < header or at + size > end:
                return
            yield kind, at + header, at + size
            at += size

    for kind, start, end in boxes(0, len(data)):
        if kind != b"moov":
            continue
        for track, track_start, track_end in boxes(start, end):
            if track != b"trak":
                continue
            for box, _, box_end in boxes(track_start, track_end):
                if box == b"tkhd" and box_end >= 8:
                    width, height = (value >> 16 for value in struct.unpack(">II", data[box_end - 8:box_end]))
                    if width and height:  # an audio track's are zero
                        return width, height
    return None


class CaptureNotFound(Exception):
    pass


@dataclass(frozen=True)
class Capture:
    """One file in the Captures list."""

    name: str
    kind: str        # "recording" or "snapshot"
    size: int        # bytes
    modified: float  # mtime, seconds since the epoch


class CaptureStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._listeners: list[Callable[[], None]] = []

    def subscribe(self, listener: Callable[[], None]) -> None:
        """Call ``listener()`` after the instance adds or deletes a Capture."""
        self._listeners.append(listener)

    def changed(self) -> None:
        """Tell listeners the Captures changed. Call it on the event loop's thread."""
        for listener in self._listeners:
            listener()

    def captures(self) -> list[Capture]:
        """Every Capture in the folder, newest first."""
        try:
            entries = list(os.scandir(self.root))
        except FileNotFoundError:
            return []
        found = []
        for entry in entries:
            match = CAPTURE_FILE.fullmatch(entry.name)
            if match is None:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:  # deleted while we looked
                continue
            if stat.S_ISREG(info.st_mode):
                found.append(Capture(entry.name, KINDS[match[1]], info.st_size, info.st_mtime))
        return sorted(found, key=lambda c: (c.modified, c.name), reverse=True)

    def find(self, name: str) -> Capture:
        """The Capture called ``name``, a name a client sent. Raises
        ``CaptureNotFound`` unless it is a Capture's name and a regular file
        directly in the folder: never a path, a dot-entry or a symlink."""
        match = CAPTURE_FILE.fullmatch(name)
        if match is None:
            raise CaptureNotFound(name)
        try:
            info = os.lstat(self.root / name)
        except (FileNotFoundError, NotADirectoryError):
            raise CaptureNotFound(name) from None
        if not stat.S_ISREG(info.st_mode):
            raise CaptureNotFound(name)
        return Capture(name, KINDS[match[1]], info.st_size, info.st_mtime)

    def path(self, capture: Capture) -> Path:
        return self.root / capture.name

    def delete(self, name: str) -> None:
        """Delete the Capture called ``name`` (see ``find``) and tell listeners.
        Whether a Recording still needs it is the caller's question."""
        capture = self.find(name)
        try:
            os.unlink(self.path(capture))  # a symlink swapped in meanwhile loses only the link
        except FileNotFoundError:
            raise CaptureNotFound(name) from None
        self.changed()

    @property
    def staging(self) -> Path:
        """Where the Relay writes Recording segments before they become Captures."""
        return self.root / STAGING

    @property
    def incoming(self) -> Path:
        """Where uploaded Snapshots are written before they become Captures."""
        return self.root / INCOMING

    def prepare(self) -> None:
        self.staging.mkdir(parents=True, exist_ok=True)
        self.incoming.mkdir(parents=True, exist_ok=True)

    def add(self, data: bytes, name: CaptureName) -> str:
        """Store ``data`` as a new Capture under ``name`` and return the file
        name it got. The caller has already checked the contents; the name
        is built here from server-side facts only, never from the upload."""
        self.incoming.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.incoming, suffix=f".{name.ext}")
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(tmp, 0o644)  # mkstemp makes it 0600; Captures are for the host's user too
            return self.place(Path(tmp), name)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def place(self, source: Path, name: CaptureName) -> str:
        """Move a finished file into the captures folder under ``name`` and
        return the file name it got. An existing Capture is never overwritten:
        a clash (a second Recording of the same Camera started within the same
        second) takes the next free ``-part-n``."""
        part = name.part or 1
        while True:
            target = self.root / name.render(part)
            try:
                # Claim the name atomically, then move the file over the claim.
                # (Not os.link: hard links are unreliable on Docker Desktop bind mounts.)
                os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            except FileExistsError:
                part += 1
                continue
            os.replace(source, target)
            return target.name
