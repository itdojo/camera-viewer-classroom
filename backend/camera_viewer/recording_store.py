"""The Recorder's file in the bind-mounted config folder.

One JSON document, ``<config dir>/recordings.json``, rewritten whole on every
change of any Recording. It holds the Recordings a restart must not forget:
the unfinished ones, so the next start can tell they were cut off, and the
Interrupted ones, so their flag outlives later restarts. A Recording that
finalizes normally leaves the file.

    {
      "version": 1,
      "recordings": [
        {
          "id": "9cedfcc20aed",
          "camera": {"device": "cam-2", "name": "csi", "source_url": "rtsps://...", "width": 1920, "height": 1080},
          "started": "2026-10-09T14:03:12.501234+02:00",
          "duration": 60,                    # seconds for a timed Recording, else null
          "state": "active",                 # waiting | active | stopping | interrupted
          "reason": null,
          "ended": null,
          "parts": 1,                        # segments begun; the open one is part `parts`
          "segment": {"path": "/captures/.recording/cam-2/csi/20261009-140312-501234.mp4",
                      "width": 1920, "height": 1080},
          "captures": ["cam-2-csi-20261009-140312-1920x1080.mp4"]
        }
      ]
    }

Written like ``devices.json`` (atomically, mode 0600). A file that cannot be
parsed is moved aside and the instance starts with no Recordings to recover;
an entry that cannot be read is skipped.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from camera_viewer.files import write_atomically
from camera_viewer.recorder import EndReason, Recording, Segment, State
from camera_viewer.registry import Camera

log = logging.getLogger("camera_viewer.recording_store")

VERSION = 1
FILE_NAME = "recordings.json"
MODE = 0o600


class RecordingStore:
    def __init__(self, config_dir: Path):
        self.path = Path(config_dir) / FILE_NAME

    def load(self) -> list[Recording]:
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return []
        try:
            entries = json.loads(raw)["recordings"]
            if not isinstance(entries, list):
                raise TypeError("recordings is not a list")
        except (ValueError, KeyError, TypeError) as exc:
            aside = self.path.with_name(f"{FILE_NAME}.unreadable-{time.strftime('%Y%m%d-%H%M%S')}")
            os.replace(self.path, aside)
            log.error("could not read %s (%s); moved it to %s", self.path, exc, aside)
            return []
        recordings = []
        for entry in entries:
            try:
                recordings.append(_recording_from(entry))
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("skipping a Recording in %s: %s", self.path, exc)
        return recordings

    def save(self, recordings: Iterable[Recording]) -> None:
        document = {"version": VERSION, "recordings": [_entry_of(r) for r in recordings]}
        write_atomically(self.path, json.dumps(document, indent=2).encode(), MODE)


def _entry_of(recording: Recording) -> dict[str, Any]:
    camera, segment = recording.camera, recording.segment
    return {
        "id": recording.id,
        "camera": {"device": camera.device, "name": camera.name, "source_url": camera.source_url,
                   "width": camera.width, "height": camera.height},
        "started": recording.started.isoformat(),
        "duration": recording.duration,
        "state": str(recording.state),
        "reason": str(recording.reason) if recording.reason else None,
        "ended": recording.ended.isoformat() if recording.ended else None,
        "parts": recording.parts,
        "segment": {"path": str(segment.path), "width": segment.width, "height": segment.height}
                   if segment else None,
        "captures": list(recording.captures),
    }


def _recording_from(entry: dict[str, Any]) -> Recording:
    camera = entry["camera"]
    segment = entry.get("segment")
    started = datetime.fromisoformat(entry["started"])
    if started.tzinfo is None:
        raise ValueError(f"started {entry['started']!r} has no time zone")
    return Recording(
        id=str(entry["id"]),
        camera=Camera(device=str(camera["device"]), name=str(camera["name"]),
                      source_url=str(camera.get("source_url", "")),
                      width=camera.get("width"), height=camera.get("height")),
        started=started,
        duration=int(entry["duration"]) if entry.get("duration") else None,
        state=State(entry["state"]),
        reason=EndReason(entry["reason"]) if entry.get("reason") else None,
        ended=datetime.fromisoformat(entry["ended"]) if entry.get("ended") else None,
        parts=int(entry.get("parts", 0)),
        segment=Segment(path=Path(segment["path"]), width=int(segment["width"]),
                        height=int(segment["height"])) if segment else None,
        captures=[str(name) for name in entry.get("captures", [])],
    )
