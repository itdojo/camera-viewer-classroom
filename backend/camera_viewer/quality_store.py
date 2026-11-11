"""Each Camera's Quality, kept in the bind-mounted config folder beside the Device store.

API changes live only in a Device's memory and Devices run no agent, so the instance is the
only place a chosen Quality survives (ADR 0009). One JSON document,
``<config dir>/quality.json``, rewritten whole and atomically on every change:

    {
      "version": 1,
      "cameras": {
        "cam-2/csi": {
          "mode": "auto",                     # "auto", or the pinned level
          "level": "medium",                  # the current level; null when unknown
          "last_write": {                     # what this instance last set; null before any
            "setting": {"width": 1920, "height": 1080, "fps": 15, "bitrate": 1500000,
                        "keyframe_interval": 15},
            "at": "2026-10-15T14:05:09.120000+00:00",
            "started": "2026-10-15T08:00:00.000000+00:00"   # the Device's /v3/info at that write
          },
          "changed_elsewhere": false          # another instance changed it since; re-apply stops
        }
      }
    }

A Camera with no entry (every Camera from before Quality existed) is on Auto at an unknown
level. An entry that can't be read is skipped with a warning, and a file that can't be parsed is
moved aside and the instance starts with every Camera on Auto, as the Device store does.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from camera_viewer.files import write_atomically
from camera_viewer.quality import Level, Setting

log = logging.getLogger("camera_viewer.quality_store")

VERSION = 1
FILE_NAME = "quality.json"
MODE = 0o600

AUTO: Literal["auto"] = "auto"
Mode = Level | Literal["auto"]
ModeText = Literal[(AUTO, *(level.value for level in Level))]  # a Mode as stored and sent


def parse_mode(text: str) -> Mode:
    """A Mode from its text, the inverse of ``str(mode)``. Raises ``ValueError`` for anything else."""
    return AUTO if text == AUTO else Level(text)


@dataclass(frozen=True)
class LastWrite:
    """What this instance last set on a Camera's Device, when, and the Device's ``started``
    then: a later ``started`` means the Device restarted and forgot it."""

    setting: Setting
    at: datetime
    started: datetime


@dataclass
class CameraQuality:
    mode: Mode = AUTO
    level: Level | None = None
    last_write: LastWrite | None = None
    changed_elsewhere: bool = False  # "changed by another instance": no re-apply until a user picks

    @property
    def auto(self) -> bool:
        return self.mode == AUTO


class QualityStore:
    def __init__(self, config_dir: Path | None = None) -> None:
        """Reads and writes ``quality.json`` in ``config_dir``; without one, nothing persists."""
        self.path = Path(config_dir) / FILE_NAME if config_dir is not None else None

    def load(self) -> dict[str, CameraQuality]:
        path = self.path
        if path is None:
            return {}
        try:
            raw = path.read_text()
        except FileNotFoundError:
            return {}
        try:
            entries = json.loads(raw)["cameras"]
            if not isinstance(entries, dict):
                raise TypeError("cameras is not an object")
        except (ValueError, KeyError, TypeError) as exc:
            aside = path.with_name(f"{FILE_NAME}.unreadable-{time.strftime('%Y%m%d-%H%M%S')}")
            os.replace(path, aside)
            log.error("could not read %s (%s); moved it to %s and put every Camera on Auto", path, exc, aside)
            return {}
        cameras = {}
        for camera_id, entry in entries.items():
            try:
                cameras[camera_id] = _quality_from(entry)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("skipping Camera %r in %s: %s", camera_id, path, exc)
        return cameras

    def save(self, cameras: Mapping[str, CameraQuality]) -> None:
        if self.path is None:
            return
        document = {"version": VERSION, "cameras": {camera_id: _entry_of(quality)
                                                    for camera_id, quality in cameras.items()}}
        write_atomically(self.path, json.dumps(document, indent=2).encode(), MODE)


def _entry_of(quality: CameraQuality) -> dict[str, Any]:
    write = quality.last_write
    return {
        "mode": str(quality.mode),
        "level": str(quality.level) if quality.level else None,
        "last_write": None if write is None else {
            "setting": write.setting.public(), "at": write.at.isoformat(), "started": write.started.isoformat()},
        "changed_elsewhere": quality.changed_elsewhere,
    }


def _quality_from(entry: Any) -> CameraQuality:
    if not isinstance(entry, dict):
        raise TypeError(f"expected an object, got {entry!r}")
    mode = entry.get("mode", AUTO)
    level = entry.get("level")
    write = entry.get("last_write")
    return CameraQuality(
        mode=parse_mode(mode),
        level=Level(level) if level is not None else None,
        last_write=None if write is None else LastWrite(
            setting=Setting.from_public(write["setting"]),
            at=_aware(write["at"]), started=_aware(write["started"])),
        changed_elsewhere=entry.get("changed_elsewhere") is True,
    )


def _aware(text: Any) -> datetime:
    value = datetime.fromisoformat(text) if isinstance(text, str) else None
    if value is None or value.tzinfo is None:
        raise ValueError(f"not a timezone-aware time: {text!r}")
    return value
