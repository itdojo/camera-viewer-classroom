"""Each Device's Fixed Position, kept in the bind-mounted config folder beside the Quality store.

A Fixed Position is entered by hand (#58), so the instance is the only place it lives. It stays
off the Device record for the reason Quality does: a write through the Device Registry would
re-declare every Relay path and re-publish ``devices``. One JSON document,
``<config dir>/positions.json``, rewritten whole and atomically on every change:

    {
      "version": 1,
      "fixed": {
        "cam-2": {"latitude": 51.47793, "longitude": -0.00148}
      }
    }

An entry is checked again on load with the rules every Position passes (``positions.check_place``),
so a hand-edited file can't put an impossible Position on screen: a bad entry is skipped with a
warning, and a file that can't be parsed is moved aside and the instance starts with none.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from pathlib import Path

from camera_viewer.files import write_atomically
from camera_viewer.positions import InvalidFix, Place, check_place

log = logging.getLogger("camera_viewer.position_store")

VERSION = 1
FILE_NAME = "positions.json"
MODE = 0o600


class PositionStore:
    def __init__(self, config_dir: Path | None = None) -> None:
        """Reads and writes ``positions.json`` in ``config_dir``; without one, nothing persists."""
        self.path = Path(config_dir) / FILE_NAME if config_dir is not None else None

    def load(self) -> dict[str, Place]:
        path = self.path
        if path is None:
            return {}
        try:
            raw = path.read_text()
        except FileNotFoundError:
            return {}
        try:
            entries = json.loads(raw)["fixed"]
            if not isinstance(entries, dict):
                raise TypeError("fixed is not an object")
        except (ValueError, KeyError, TypeError) as exc:
            aside = path.with_name(f"{FILE_NAME}.unreadable-{time.strftime('%Y%m%d-%H%M%S')}")
            os.replace(path, aside)
            log.error("could not read %s (%s); moved it to %s and started with no Fixed Positions",
                      path, exc, aside)
            return {}
        fixed = {}
        for label, entry in entries.items():
            try:
                if not isinstance(entry, dict):
                    raise InvalidFix(f"expected an object, got {str(entry)[:40]!r}")
                fixed[label] = check_place(entry.get("latitude"), entry.get("longitude"))
            except InvalidFix as exc:
                log.warning("skipping the Fixed Position of %r in %s: %s", label, path, exc)
        return fixed

    def save(self, fixed: Mapping[str, Place]) -> None:
        if self.path is None:
            return
        document = {"version": VERSION, "fixed": {label: place.public() for label, place in fixed.items()}}
        write_atomically(self.path, json.dumps(document, indent=2).encode(), MODE)
