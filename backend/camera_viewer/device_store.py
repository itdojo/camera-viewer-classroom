"""The Device Registry's file in the bind-mounted config folder.

One JSON document, ``<config dir>/devices.json``, rewritten whole on every
change. Credentials are in it in plain text (the Device Kit's instance user
and password, and any password in a manual Camera's URL), so the file is
created mode 0600 and kept that way; the README says so.

    {
      "version": 1,
      "devices": [
        {
          "label": "cam-2",
          "address": "192.0.2.40",
          "username": "viewer",
          "password": "...",
          "fingerprint": "<sha-256 hex, or empty for a plain-RTSP manual Device>",
          "api": true,
          "cameras": [
            {"name": "csi", "source_url": "rtsps://192.0.2.40:8322/csi", "width": 1920, "height": 1080}
          ]
        }
      ]
    }

``api`` is false for a Device created from a manual Camera: the instance has
no Device API to talk to there. A Camera's ``source_url`` never carries
credentials; the Relay Controller adds the Device's.

Writes are atomic (a 0600 temporary file, fsynced, renamed over the old one),
so a container killed mid-write leaves the previous file intact. A file that
cannot be parsed is moved aside, never overwritten, and the instance starts
empty, so a maintainer can still recover it by hand.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from camera_viewer.files import write_atomically
from camera_viewer.registry import (
    Camera,
    Device,
    RegistryError,
    check_camera_name,
    check_label,
)

log = logging.getLogger("camera_viewer.device_store")

VERSION = 1
FILE_NAME = "devices.json"
MODE = 0o600


class DeviceStore:
    def __init__(self, config_dir: Path):
        self.path = Path(config_dir) / FILE_NAME

    def load(self) -> list[Device]:
        """Every Device in the file; none if there is no file yet. Entries
        that fail the Registry's own checks (a hand-edited file is as
        untrusted as a Device) are skipped with a warning."""
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return []
        os.chmod(self.path, MODE)  # a file put there by hand may be looser
        try:
            document = json.loads(raw)
            entries = document["devices"]
            if not isinstance(entries, list):
                raise TypeError("devices is not a list")
        except (ValueError, KeyError, TypeError) as exc:
            aside = self.path.with_name(f"{FILE_NAME}.unreadable-{time.strftime('%Y%m%d-%H%M%S')}")
            os.replace(self.path, aside)
            log.error("could not read %s (%s); moved it to %s and started with no Devices",
                      self.path, exc, aside)
            return []
        devices, labels = [], set()
        for entry in entries:
            try:
                device = _device_from(entry)
            except (RegistryError, KeyError, TypeError, ValueError) as exc:
                log.warning("skipping a Device in %s: %s", self.path, exc)
                continue
            if device.label in labels:
                log.warning("skipping a second Device labelled %r in %s", device.label, self.path)
                continue
            labels.add(device.label)
            devices.append(device)
        return devices

    def save(self, devices: Iterable[Device]) -> None:
        document = {"version": VERSION, "devices": [_entry_of(device) for device in devices]}
        write_atomically(self.path, json.dumps(document, indent=2).encode(), MODE)


def _entry_of(device: Device) -> dict[str, Any]:
    return {
        "label": device.label,
        "address": device.address,
        "username": device.username,
        "password": device.password,
        "fingerprint": device.fingerprint,
        "api": device.api,
        "cameras": [
            {"name": camera.name, "source_url": camera.source_url,
             "width": camera.width, "height": camera.height}
            for camera in device.cameras
        ],
    }


def _device_from(entry: dict[str, Any]) -> Device:
    label = check_label(entry["label"])
    cameras = []
    for item in entry.get("cameras", []):
        name = check_camera_name(item["name"])
        cameras.append(Camera(device=label, name=name, source_url=_str(item["source_url"]),
                              width=_int(item.get("width")), height=_int(item.get("height"))))
    return Device(label=label, address=_str(entry["address"]), username=_str(entry.get("username", "")),
                  password=_str(entry.get("password", "")), fingerprint=_str(entry.get("fingerprint", "")),
                  api=bool(entry.get("api", True)), cameras=tuple(cameras))


def _str(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {value!r}")
    return value


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
