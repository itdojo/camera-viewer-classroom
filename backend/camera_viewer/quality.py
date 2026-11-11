"""Quality translation: a level, the ladder it sits on, and the Device config that sets it.

Pure functions and data, no I/O (ADR 0009). The Quality Service (``quality_service.py``)
reads a Camera's path config from its Device, asks this module what kit the path is and what
to PATCH, and compares what the Device reports with what it last wrote.

Kits. A path whose ``source`` is ``rpiCamera`` is the CSI kit: a level is five ``rpiCamera*``
fields. A path whose ``runOnInit`` runs ffmpeg with a size, a frame rate and a ``-b:v`` is the
ffmpeg kind (the USB kit, and the test-pattern kit, which shares the USB ladder): a level is the
same command with only its size, frame rate, GOP, bitrate, maxrate and bufsize arguments
replaced. Every other token, and the whitespace between tokens, is kept byte for byte, including
the device node and ``$USB_CAMERA_FILTER`` (MediaMTX expands it when it runs the command). The
size and rate are ``-video_size``/``-framerate`` for a capture device, or the ``size=``/``rate=``
options of a lavfi source such as ``testsrc``. Any other path has no Quality.

The ladder is data: four rungs per kit, High to Minimum, each marking whether the step down to
the next rung drops viewers. On CSI only width and height recreate the path, so only the step
between 1080p and 720p does; on ffmpeg kits every change restarts ffmpeg. The keyframe interval
equals the rung's fps, so a Feed gets one keyframe a second. The values are a starting point,
tuned on hardware in #50.

Everything here reads Device config, and a Device is untrusted input (CODING_STANDARDS rule 1),
so a value of the wrong type or out of range reads as no setting rather than raising.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from typing import Any

from camera_viewer.device_client import dimension


class Level(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    MINIMUM = "minimum"


class Kit(StrEnum):
    CSI = "csi"        # MediaMTX's rpiCamera source
    FFMPEG = "ffmpeg"  # a runOnInit ffmpeg publishing to the path: the USB and test-pattern kits


MAX_FPS = 240
MAX_BITRATE = 1_000_000_000


def _whole(value: Any, top: int = MAX_BITRATE) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= top


@dataclass(frozen=True)
class Setting:
    """What a Camera encodes at. ``bitrate`` in bits per second; ``keyframe_interval`` in frames
    (``rpiCameraIDRPeriod`` or ffmpeg's ``-g``), or None when the command sets none."""

    width: int
    height: int
    fps: int
    bitrate: int
    keyframe_interval: int | None

    def public(self) -> dict[str, Any]:
        return {"width": self.width, "height": self.height, "fps": self.fps, "bitrate": self.bitrate,
                "keyframe_interval": self.keyframe_interval}

    @classmethod
    def from_public(cls, data: Any) -> Setting:
        """The inverse of ``public``. Raises ``TypeError`` on anything else (a hand-edited store)."""
        if not isinstance(data, Mapping):
            raise TypeError(f"not a Quality setting: {data!r}")
        setting = _setting(data.get("width"), data.get("height"), data.get("fps"), data.get("bitrate"),
                           data.get("keyframe_interval"))
        if setting is None:
            raise TypeError(f"not a Quality setting: {data!r}")
        return setting


def _setting(width: Any, height: Any, fps: Any, bitrate: Any, interval: Any) -> Setting | None:
    if not (dimension(width) and dimension(height) and _whole(fps, MAX_FPS)
            and _whole(bitrate) and (interval is None or _whole(interval, 100_000))):
        return None
    return Setting(width, height, fps, bitrate, interval)


@dataclass(frozen=True)
class Rung:
    level: Level
    setting: Setting
    disruptive_below: bool  # stepping from here to the next rung down drops viewers


@dataclass(frozen=True)
class Ladder:
    kit: Kit
    rungs: tuple[Rung, ...]  # High first, Minimum last

    def setting(self, level: Level) -> Setting:
        return self.rungs[self._index(level)].setting

    def level_of(self, setting: Setting | None) -> Level | None:
        """The level whose setting this is exactly, or None when it is off the ladder."""
        return next((rung.level for rung in self.rungs if rung.setting == setting), None)

    def below(self, level: Level) -> Level | None:
        index = self._index(level)
        return self.rungs[index + 1].level if index + 1 < len(self.rungs) else None

    def above(self, level: Level) -> Level | None:
        index = self._index(level)
        return self.rungs[index - 1].level if index > 0 else None

    def disrupts(self, current: Level | None, target: Level, *, current_setting: Setting | None = None) -> bool:
        """Whether moving from ``current`` to ``target`` drops viewers: it crosses a step marked
        disruptive. From a level off the ladder (``current`` None), an ffmpeg kit always restarts,
        and CSI drops viewers unless ``current_setting`` already has the target's resolution."""
        if current is None:
            if self.kit is Kit.FFMPEG or current_setting is None:
                return True
            wanted = self.setting(target)
            return (current_setting.width, current_setting.height) != (wanted.width, wanted.height)
        low, high = sorted((self._index(current), self._index(target)))
        return any(rung.disruptive_below for rung in self.rungs[low:high])

    def _index(self, level: Level) -> int:
        return next(i for i, rung in enumerate(self.rungs) if rung.level == level)


def _rung(level: Level, width: int, height: int, fps: int, bitrate: int, disruptive_below: bool) -> Rung:
    return Rung(level, Setting(width, height, fps, bitrate, fps), disruptive_below)


# ADR 0009's table. Only CSI's Medium-to-Low step changes resolution.
CSI_LADDER = Ladder(Kit.CSI, (
    _rung(Level.HIGH, 1920, 1080, 30, 4_000_000, disruptive_below=False),
    _rung(Level.MEDIUM, 1920, 1080, 15, 1_500_000, disruptive_below=True),
    _rung(Level.LOW, 1280, 720, 15, 800_000, disruptive_below=False),
    _rung(Level.MINIMUM, 1280, 720, 5, 300_000, disruptive_below=False),
))
USB_LADDER = Ladder(Kit.FFMPEG, (
    _rung(Level.HIGH, 1280, 720, 30, 2_000_000, disruptive_below=True),
    _rung(Level.MEDIUM, 1280, 720, 15, 1_000_000, disruptive_below=True),
    _rung(Level.LOW, 640, 360, 15, 500_000, disruptive_below=True),
    _rung(Level.MINIMUM, 640, 360, 10, 250_000, disruptive_below=False),
))
LADDERS: dict[Kit, Ladder] = {Kit.CSI: CSI_LADDER, Kit.FFMPEG: USB_LADDER}


# -- reading a Device's path config --------------------------------------------------------------

def kit_of(config: Any) -> Kit | None:
    """Which kit a path config (``/v3/config/paths/get/{name}``) is, or None for no Quality."""
    if not isinstance(config, Mapping):
        return None
    if config.get("source") == "rpiCamera":
        return Kit.CSI
    if _ffmpeg_setting(config.get("runOnInit")) is not None:
        return Kit.FFMPEG
    return None


def live_setting(kit: Kit, config: Any) -> Setting | None:
    """What the path config says the Camera encodes at, or None if it can't be read."""
    if not isinstance(config, Mapping):
        return None
    if kit is Kit.CSI:
        return _setting(config.get("rpiCameraWidth"), config.get("rpiCameraHeight"), config.get("rpiCameraFPS"),
                        config.get("rpiCameraBitrate"), config.get("rpiCameraIDRPeriod"))
    return _ffmpeg_setting(config.get("runOnInit"))


def patch_for(kit: Kit, level: Level, config: Mapping[str, Any]) -> dict[str, Any]:
    """The ``/v3/config/paths/patch/{name}`` body that sets ``level`` on this path. For an ffmpeg
    kit, ``config`` must be the path's live config, whose command is rewritten."""
    setting = LADDERS[kit].setting(level)
    if kit is Kit.CSI:
        return {"rpiCameraWidth": setting.width, "rpiCameraHeight": setting.height,
                "rpiCameraFPS": setting.fps, "rpiCameraBitrate": setting.bitrate,
                "rpiCameraIDRPeriod": setting.keyframe_interval}
    command = config.get("runOnInit")
    if _ffmpeg_setting(command) is None:
        raise ValueError("the path's runOnInit is not an ffmpeg command this kit can rewrite")
    return {"runOnInit": _rewrite(command, setting)}


# -- ffmpeg commands --------------------------------------------------------------------------------

_TOKEN = re.compile(r"\S+")
_SIZE = re.compile(r"(\d+)x(\d+)")
_BITRATE = re.compile(r"(\d+(?:\.\d+)?)([kKM]?)")
_LAVFI_SIZE = re.compile(r"(?<=[=:])size=(\d+x\d+)")
_LAVFI_RATE = re.compile(r"(?<=[=:])rate=(\d+)")
_MULTIPLIER = {"": 1, "k": 1000, "K": 1000, "M": 1_000_000}


def _values(command: str) -> dict[str, re.Match]:
    """The value token after each option this module reads, first occurrence of each, plus the
    lavfi source after ``-f lavfi -i`` as ``lavfi``."""
    tokens = list(_TOKEN.finditer(command))
    found: dict[str, re.Match] = {}
    for option, value in pairwise(tokens):
        name = option.group()
        if name in ("-video_size", "-framerate", "-g", "-b:v", "-maxrate", "-bufsize") and name not in found:
            found[name] = value
    for f, kind, i, source in zip(tokens, tokens[1:], tokens[2:], tokens[3:], strict=False):
        if (f.group(), kind.group(), i.group()) == ("-f", "lavfi", "-i") and "lavfi" not in found:
            found["lavfi"] = source
    return found


def _bitrate(text: str) -> int | None:
    match = _BITRATE.fullmatch(text)
    if not match:
        return None
    value = round(float(match.group(1)) * _MULTIPLIER[match.group(2)])
    return value if _whole(value) else None


def _whole_text(text: str | None) -> int | None:
    return int(text) if text is not None and text.isdigit() and len(text) < 7 else None


def _ffmpeg_setting(command: Any) -> Setting | None:
    if not isinstance(command, str):
        return None
    first = _TOKEN.search(command)
    if first is None or os.path.basename(first.group()) != "ffmpeg":
        return None
    found = _values(command)
    size = found["-video_size"].group() if "-video_size" in found else None
    rate = found["-framerate"].group() if "-framerate" in found else None
    if "lavfi" in found and size is None and rate is None:
        lavfi = found["lavfi"].group()
        size = m.group(1) if (m := _LAVFI_SIZE.search(lavfi)) else None
        rate = m.group(1) if (m := _LAVFI_RATE.search(lavfi)) else None
    size_match = _SIZE.fullmatch(size) if size else None
    if size_match is None or "-b:v" not in found:
        return None
    interval = _whole_text(found["-g"].group()) if "-g" in found else None
    return _setting(_whole_text(size_match.group(1)), _whole_text(size_match.group(2)), _whole_text(rate),
                    _bitrate(found["-b:v"].group()), interval)


def _rate_text(bits: int) -> str:
    """A bitrate as ffmpeg's kit strings write it: ``2M``, ``500k``."""
    if bits % 1_000_000 == 0:
        return f"{bits // 1_000_000}M"
    if bits % 1000 == 0:
        return f"{bits // 1000}k"
    return str(bits)


def _rewrite(command: str, setting: Setting) -> str:
    found = _values(command)
    size = f"{setting.width}x{setting.height}"
    replacements: dict[str, str] = {
        "-g": str(setting.keyframe_interval or setting.fps),
        "-b:v": _rate_text(setting.bitrate),
        "-maxrate": _rate_text(setting.bitrate),
        "-bufsize": _rate_text(setting.bitrate // 2),
    }
    if "-video_size" in found:
        replacements["-video_size"] = size
    if "-framerate" in found:
        replacements["-framerate"] = str(setting.fps)
    if "lavfi" in found and "-video_size" not in found and "-framerate" not in found:
        lavfi = found["lavfi"].group()
        lavfi = _LAVFI_SIZE.sub(f"size={size}", lavfi)
        replacements["lavfi"] = _LAVFI_RATE.sub(f"rate={setting.fps}", lavfi)
    edits = sorted(((found[key].span(), text) for key, text in replacements.items() if key in found),
                   reverse=True)
    for (start, end), text in edits:
        command = command[:start] + text + command[end:]
    return command
