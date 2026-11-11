"""Disk Monitor: free space on the filesystem holding the captures folder,
and the thresholds that keep it from filling.

Each threshold does one job, checked on its own:

- **warn** below ``warn_percent`` of the total or below ``warn_bytes`` free;
- **refuse** new Recordings and Snapshots below ``refuse_bytes``;
- **finalize** (the low-disk cutoff) every active Recording below
  ``finalize_bytes``, so the Relay closes each file properly before the disk
  is full.

With the defaults (10%, 5 GB, 1 GB, 500 MB) each level implies the ones
before it. The reported ``level`` is the most severe one crossed.

The monitor measures every ``interval`` seconds and tells its subscribers
after every measurement, so every browser holds a reading at most one
interval old. A caller about to write (a Recording start, a Snapshot upload)
measures afresh with ``check()`` rather than trusting the last reading.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple

log = logging.getLogger(__name__)

_UNITS = {"": 1, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15}
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGTP]?)B?\s*$", re.IGNORECASE)


def parse_size(text: str) -> int:
    """``"500M"`` or ``"500MB"`` -> 500_000_000; a bare number is bytes.
    Units are decimal (K = 1000), like the GB a disk is sold in."""
    match = _SIZE.match(text)
    if not match:
        raise ValueError(f"not a size: {text!r} (use bytes, or a number with K, M, G or T, e.g. 5G)")
    return int(float(match[1]) * _UNITS[match[2].upper()])


class Level(StrEnum):
    OK = "ok"
    WARN = "warn"
    REFUSE = "refuse"
    FINALIZE = "finalize"


@dataclass(frozen=True)
class DiskThresholds:
    warn_percent: float = 10.0
    warn_bytes: int = 5 * 10**9
    refuse_bytes: int = 10**9
    finalize_bytes: int = 500 * 10**6

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> DiskThresholds:
        """Read overrides from the ``CV_DISK_*`` variables; unset ones keep their defaults."""
        names = {"warn_percent": "CV_DISK_WARN_PERCENT", "warn_bytes": "CV_DISK_WARN_BYTES",
                 "refuse_bytes": "CV_DISK_REFUSE_BYTES", "finalize_bytes": "CV_DISK_FINALIZE_BYTES"}
        overrides: dict[str, Any] = {}
        for field in fields(cls):
            name = names[field.name]
            if not env.get(name, "").strip():
                continue
            try:
                value = float(env[name]) if field.name == "warn_percent" else parse_size(env[name])
            except ValueError as exc:
                raise ValueError(f"{name}: {exc}") from None
            if value < 0 or (field.name == "warn_percent" and value > 100):
                raise ValueError(f"{name} is out of range: {env[name]!r}")
            overrides[field.name] = value
        return cls(**overrides)

    def public(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


class Usage(NamedTuple):
    total: int
    used: int
    free: int


@dataclass(frozen=True)
class DiskSpace:
    """One measurement, judged against the thresholds."""

    free: int
    total: int
    checked: datetime
    thresholds: DiskThresholds

    @property
    def refusing(self) -> bool:
        """New Recordings and Snapshots are refused."""
        return self.free < self.thresholds.refuse_bytes

    @property
    def finalizing(self) -> bool:
        """The low-disk cutoff: active Recordings are stopped and finalized."""
        return self.free < self.thresholds.finalize_bytes

    @property
    def level(self) -> Level:
        t = self.thresholds
        if self.finalizing:
            return Level.FINALIZE
        if self.refusing:
            return Level.REFUSE
        if self.free < t.warn_bytes or self.free * 100 < self.total * t.warn_percent:
            return Level.WARN
        return Level.OK

    def refusal_message(self) -> str:
        return (f"Only {human(self.free)} free on the captures disk: new Recordings and Snapshots "
                f"are refused below {human(self.thresholds.refuse_bytes)}. "
                "Delete Captures to make room.")

    def public(self) -> dict[str, Any]:
        return {"free": self.free, "total": self.total, "level": str(self.level),
                "refusing": self.refusing, "checked": self.checked.isoformat(),
                "thresholds": self.thresholds.public()}


def human(size: int) -> str:
    """``1_234_000_000`` -> ``"1.2 GB"`` (decimal units, as thresholds are given)."""
    for unit, factor in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("kB", 10**3)):
        if size >= factor:
            return f"{size / factor:.1f} {unit}"
    return f"{size} B"


Listener = Callable[[DiskSpace], None]


class DiskMonitor:
    def __init__(self, path: Path, thresholds: DiskThresholds, *, interval: float,
                 usage: Callable[[Path], Usage] = shutil.disk_usage,
                 clock: Callable[[], datetime] = lambda: datetime.now().astimezone()) -> None:
        self._path = Path(path)
        self._thresholds = thresholds
        self._interval = interval
        self._usage = usage
        self._clock = clock
        self._last: DiskSpace | None = None
        self._listeners: list[Listener] = []
        self._cutoff: list[Callable[[], None]] = []

    def subscribe(self, listener: Listener) -> None:
        """Call ``listener(space)`` after every measurement."""
        self._listeners.append(listener)

    def on_cutoff(self, action: Callable[[], None]) -> None:
        """Run ``action`` after every measurement below the finalize threshold."""
        self._cutoff.append(action)

    def current(self) -> DiskSpace:
        """The last measurement, or a fresh one if there is none yet."""
        if self._last is None:
            self._last = self._measure()
        return self._last

    def _measure(self) -> DiskSpace:
        usage = self._usage(self._path)
        return DiskSpace(free=usage.free, total=usage.total, checked=self._clock(),
                         thresholds=self._thresholds)

    def check(self) -> DiskSpace:
        """Measure now, tell subscribers, and run the cutoff if it is due."""
        space = self._measure()
        if self._last is None or space.level is not self._last.level:
            log.log(logging.INFO if space.level is Level.OK else logging.WARNING,
                    "captures disk: %s free of %s, level %s", human(space.free), human(space.total), space.level)
        self._last = space
        for listener in self._listeners:
            listener(space)
        if space.finalizing:
            for action in self._cutoff:
                action()
        return space

    async def run(self) -> None:
        while True:
            try:
                self.check()
            except OSError as exc:
                log.warning("cannot measure free space of %s: %s", self._path, exc)
            await asyncio.sleep(self._interval)
