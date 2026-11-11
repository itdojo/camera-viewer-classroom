"""Recorder: owns the Recording lifecycle.

A Recording belongs to the instance, not to a browser: it runs here until it
is stopped, whoever started it and whatever their tab does.

The Relay does the writing (native fragmented MP4, no transcoding). The
Recorder only says which Camera paths record and hold their Feed open; the
app turns ``recorded()`` and ``held()`` into Relay paths. The Relay reports
back through its segment hooks, which reach ``segment_created`` and
``segment_completed``; a completed segment is moved into the captures folder
under its Capture name.

States::

    start ──> waiting ──segment created──> active
                 ^                            │
                 └──── segment completed ─────┘   (Feed dropped: next file is -part-n)
    waiting/active ──stop──> stopping ──Relay stopped recording, every file placed──> finalized
    (instance restarted while recording) ──> interrupted

A stop always goes through stopping, even with no file known to be open:
the Relay may already be writing one whose "created" hook hasn't arrived.
The Recording finalizes once the Relay has applied "record off" for its
path and every file it opened since the Recording started has been placed.
Until then the Camera can't start another Recording, so no late hook is
mistaken for the next one's. A segment belongs to the Recording that knows
its file, else to the Camera's latest Recording started before the file
(whose name carries the time the Relay opened it).

A timed Recording carries its ``duration``; ``RecordingTimers`` (timers.py)
stops it with ``EndReason.TIMER`` when that much wall-clock time has passed
since ``started``. A finalized Recording carries why it ended: stopped by a
user, its timer, its Device's removal, or low disk. One Camera has at most one unfinished
Recording at a time; several Cameras record at once.

Every change is written to the journal (``recording_store``) before anyone
hears of it. ``recover()``, at startup, turns what the last run left
unfinished into Interrupted Recordings: their staged files become Captures
and nothing about them ever records again. Then every file still staged,
whoever wrote it, is placed as a Capture too, so a lost hook never strands
footage in ``.recording/``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from camera_viewer.captures import CaptureName, CaptureStore, mp4_video_size
from camera_viewer.registry import Camera

log = logging.getLogger("camera_viewer.recorder")

STOP_TIMEOUT_SECONDS = 15.0
# After the Relay accepts "record off", how long it may take to close the
# open file (its config reload is asynchronous) before staging is checked.
RECORD_OFF_GRACE_SECONDS = 0.5
# The Relay's segment file name: recordPath ends in %Y%m%d-%H%M%S-%f (app.record_settings).
SEGMENT_STAMP = "%Y%m%d-%H%M%S-%f"


class State(StrEnum):
    WAITING = "waiting"          # recording, but no Feed is reaching the file (yet, or since a drop)
    ACTIVE = "active"            # the Relay is writing a segment
    STOPPING = "stopping"        # told to end; the Relay is finalizing the last segment
    FINALIZED = "finalized"
    INTERRUPTED = "interrupted"  # the instance stopped mid-Recording; never resumed


UNFINISHED = (State.WAITING, State.ACTIVE, State.STOPPING)


class EndReason(StrEnum):
    STOPPED = "stopped"
    TIMER = "timer"
    DEVICE_REMOVED = "device_removed"
    LOW_DISK = "low_disk"


@dataclass
class Segment:
    """One file the Relay is writing for a Recording."""

    path: Path
    width: int
    height: int


@dataclass
class Recording:
    id: str
    camera: Camera
    started: datetime            # timezone-aware; names every file of this Recording
    duration: int | None = None  # seconds, for a timed Recording; None runs until stopped
    state: State = State.WAITING
    reason: EndReason | None = None
    ended: datetime | None = None
    parts: int = 0               # segments begun so far; the next file is part parts+1
    segment: Segment | None = None
    captures: list[str] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return self.state not in UNFINISHED

    @property
    def ends(self) -> datetime | None:
        """When a timed Recording's timer runs out: wall-clock time, Feed or no Feed."""
        return self.started + timedelta(seconds=self.duration) if self.duration else None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "camera": self.camera.id,
            "state": str(self.state),
            "reason": str(self.reason) if self.reason else None,
            "started": self.started.isoformat(timespec="seconds"),
            "ended": self.ended.isoformat(timespec="seconds") if self.ended else None,
            **self._timer(),
            "captures": list(self.captures),
        }

    def _timer(self) -> dict[str, Any]:
        """``duration``, ``ends`` and the seconds ``remaining`` (as of now, for a
        countdown that doesn't depend on the browser's clock); all None untimed."""
        ends = self.ends
        if ends is None:
            return {"duration": None, "ends": None, "remaining": None}
        remaining = 0.0 if self.finished else (ends - datetime.now(ends.tzinfo)).total_seconds()
        return {"duration": self.duration, "ends": ends.isoformat(timespec="milliseconds"),
                "remaining": round(max(remaining, 0.0), 1)}


class AlreadyRecording(Exception):
    def __init__(self, recording: Recording):
        super().__init__(f"{recording.camera.id} is already recording")
        self.recording = recording


Resolution = Callable[[str], Awaitable[tuple[int, int] | None]]
RecordOff = Callable[[str], Awaitable[None]]
Listener = Callable[[], None]


async def _no_relay(camera_id: str) -> None:
    return None


def segment_started(path: Path) -> datetime | None:
    """When the Relay opened a segment file, from its name, in local time."""
    try:
        return datetime.strptime(path.stem, SEGMENT_STAMP).astimezone()
    except ValueError:
        return None


class Journal(Protocol):
    """Where the Recordings a restart must not forget are kept
    (``recording_store.RecordingStore``, the config folder)."""

    def load(self) -> list[Recording]: ...
    def save(self, recordings: Iterable[Recording]) -> None: ...


class Recorder:
    def __init__(self, store: CaptureStore, resolution: Resolution, *,
                 journal: Journal | None = None,
                 record_off: RecordOff = _no_relay,
                 stop_timeout: float = STOP_TIMEOUT_SECONDS,
                 clock: Callable[[], datetime] = lambda: datetime.now().astimezone()) -> None:
        """``resolution(camera_id)`` reads the received resolution of a Camera's
        Feed from the Relay (the track's codec properties; the Relay's record
        path has no resolution variable), or None if the Relay has no track.
        ``record_off(camera_id)`` returns once the Relay has applied the
        Camera path's current config, which for a stopping Recording has
        recording off. Without a ``journal`` nothing survives a restart."""
        self._store = store
        self._journal = journal
        self._resolution = resolution
        self._record_off = record_off
        self._stop_timeout = stop_timeout
        self._clock = clock
        self._recordings: dict[str, Recording] = {}
        self._latest: dict[str, Recording] = {}  # camera id -> its most recent Recording
        self._listeners: list[Listener] = []
        self._settling: dict[str, asyncio.Task] = {}  # recording id -> its wind-down
        self._changes = asyncio.Event()  # set on every change, for waiting wind-downs

    # -- observing ----------------------------------------------------------------

    def subscribe(self, listener: Listener) -> None:
        """Call ``listener()`` after every change of any Recording."""
        self._listeners.append(listener)

    def get(self, recording_id: str) -> Recording | None:
        return self._recordings.get(recording_id)

    def unfinished(self) -> list[Recording]:
        return [r for r in self._recordings.values() if not r.finished]

    def for_camera(self, camera_id: str) -> Recording | None:
        """The Camera's unfinished Recording, if any."""
        recording = self._latest.get(camera_id)
        return recording if recording and not recording.finished else None

    def interrupted(self) -> list[Recording]:
        """Recordings cut off by the instance stopping. One is forgotten at a
        later startup once none of its Captures is left in the folder."""
        return [r for r in self._recordings.values() if r.state is State.INTERRUPTED]

    def recorded(self) -> set[str]:
        """Camera ids whose Relay path should be recording right now."""
        return {r.camera.id for r in self.unfinished() if r.state is not State.STOPPING}

    def held(self) -> set[str]:
        """Camera ids whose Feed must stay pulled with no browser watching."""
        return {r.camera.id for r in self.unfinished()}

    # -- commands -------------------------------------------------------------------

    def start(self, camera: Camera, duration: int | None = None) -> Recording:
        """Start recording ``camera``, until stopped or for ``duration`` seconds."""
        if (current := self.for_camera(camera.id)) is not None:
            raise AlreadyRecording(current)
        recording = Recording(id=secrets.token_hex(6), camera=camera, started=self._clock(),
                              duration=duration)
        self._recordings[recording.id] = recording
        self._latest[camera.id] = recording
        log.info("recording %s of %s started", recording.id, camera.id)
        self._changed()
        return recording

    async def stop(self, recording_id: str, reason: EndReason = EndReason.STOPPED) -> Recording:
        """End a Recording and wait until its last file is in the captures
        folder. One already finished (or Interrupted) is returned as it is."""
        recording = self._recordings[recording_id]
        if not recording.finished:
            self._end(recording, reason)
            await asyncio.shield(self._settle(recording))
        return recording

    async def finish(self, cameras: Iterable[Camera], reason: EndReason) -> None:
        """End the unfinished Recordings of ``cameras`` and wait until each is
        finalized (at most the stop timeout), e.g. before a Device's paths go away."""
        ending = []
        for camera in cameras:
            if (recording := self.for_camera(camera.id)) is not None:
                self._end(recording, reason)
                ending.append(self._settle(recording))
        if ending:
            await asyncio.shield(asyncio.gather(*ending))

    def end_recordings(self, cameras: Iterable[Camera], reason: EndReason) -> None:
        """End the unfinished Recordings of ``cameras`` without waiting for
        their files; the wait runs in the background."""
        for camera in cameras:
            if (recording := self.for_camera(camera.id)) is not None:
                self._end(recording, reason)
                self._settle(recording)

    def recover(self) -> None:
        """At startup, before anything records: take back what the last run
        left in the journal. A Recording it left unfinished was cut off by the
        instance stopping, so it becomes Interrupted, never resumes, and every
        file it had staged is placed as a Capture. An Interrupted Recording
        whose Captures have all been deleted is forgotten. Then every file
        still staged is placed on its own (``_place_orphans``)."""
        if self._journal is not None:
            for recording in self._journal.load():
                if not recording.finished:
                    self._interrupt(recording)
                elif not any((self._store.root / name).exists() for name in recording.captures):
                    continue
                self._recordings[recording.id] = recording
                self._latest[recording.camera.id] = recording
        self._place_orphans()
        self._changed()

    # -- the Relay's segment hooks ----------------------------------------------------

    async def segment_created(self, camera_id: str, path: Path) -> None:
        recording = self._owner(camera_id, path)
        if recording is None or recording.finished or not path.exists():
            log.warning("segment %s created on %s with no unfinished Recording to take it", path, camera_id)
            return
        if recording.segment is not None and recording.segment.path == path:
            return  # its "completed" hook got here first and is placing it
        # Known before anything awaits, so a "completed" hook arriving
        # meanwhile finds it; its size is filled in below.
        segment = Segment(path=path, width=0, height=0)
        recording.segment = segment
        recording.parts += 1
        if recording.state is State.WAITING:
            recording.state = State.ACTIVE
        self._changed()
        size = await self._resolution(camera_id)
        if size and not segment.width:
            segment.width, segment.height = size
            if recording.segment is segment:
                self._changed()

    async def segment_completed(self, camera_id: str, path: Path) -> None:
        recording = self._owner(camera_id, path)
        if recording is None or recording.finished or not path.exists():
            log.warning("segment %s completed on %s with no unfinished Recording to take it; "
                        "the next start places it", path, camera_id)
            return
        segment = recording.segment if recording.segment and recording.segment.path == path else None
        if segment is None:  # its create hook never arrived (one arriving later is ignored)
            recording.parts += 1
            segment = Segment(path=path, width=0, height=0)
            recording.segment = segment  # claimed, so nothing else takes it while we read its size
        if not segment.width and (size := await self._resolution(camera_id)) and not segment.width:
            segment.width, segment.height = size
        if not path.exists():  # placed by a stop that timed out meanwhile
            return
        recording.captures.append(self._store.place(path, self._name(recording, segment)))
        if recording.segment is segment:
            recording.segment = None
        if recording.state is State.ACTIVE:
            recording.state = State.WAITING  # the Feed dropped; the next segment is the next part
        self._changed()

    # -- internals ----------------------------------------------------------------------

    def _owner(self, camera_id: str, path: Path) -> Recording | None:
        """The Recording a segment file belongs to: the one that knows it, else
        the Camera's latest Recording started before the Relay opened it."""
        mine = [r for r in self._recordings.values() if r.camera.id == camera_id]
        for recording in mine:
            if recording.segment is not None and recording.segment.path == path:
                return recording
        opened = segment_started(path)
        if opened is None:
            return self._latest.get(camera_id)
        return max((r for r in mine if r.started <= opened), key=lambda r: r.started, default=None)

    def _end(self, recording: Recording, reason: EndReason) -> None:
        """Tell an unfinished Recording to end: the Relay stops recording its
        path, and ``_settle`` finalizes it once every file is placed."""
        if recording.finished or recording.state is State.STOPPING:
            return
        recording.reason = reason
        recording.state = State.STOPPING
        self._changed()

    def _settle(self, recording: Recording) -> asyncio.Task:
        """The one background wind-down of a stopping Recording."""
        task = self._settling.get(recording.id)
        if task is None:
            task = asyncio.get_running_loop().create_task(self._wind_down(recording),
                                                          name=f"settle-{recording.id}")
            self._settling[recording.id] = task
            task.add_done_callback(lambda _: self._settling.pop(recording.id, None))
        return task

    async def _wind_down(self, recording: Recording) -> None:
        """Wait until the Relay has stopped recording the path and every file
        it wrote for this Recording is placed. If it never reports one (its
        path went away with a removed Device, a hook failed), place whatever
        it wrote and finalize anyway."""
        try:
            await asyncio.wait_for(self._files_placed(recording), self._stop_timeout)
        except TimeoutError:
            if recording.finished:
                return
            staged = self._staged(recording)
            log.warning("recording %s: the Relay never reported %s; ending it anyway",
                        recording.id, [str(p) for p in staged])
            for path in staged:
                segment = (recording.segment if recording.segment and recording.segment.path == path
                           else None)
                if segment is None:
                    recording.parts += 1
                    segment = Segment(path=path, width=0, height=0)
                recording.captures.append(self._store.place(path, self._name(recording, segment)))
            recording.segment = None
        if not recording.finished:
            self._finalize(recording)

    async def _files_placed(self, recording: Recording) -> None:
        await self._record_off(recording.camera.id)
        await asyncio.sleep(RECORD_OFF_GRACE_SECONDS)
        while recording.segment is not None or self._staged(recording):
            self._changes.clear()
            try:
                await asyncio.wait_for(self._changes.wait(), 0.5)
            except TimeoutError:
                pass

    def _staged(self, recording: Recording) -> list[Path]:
        """Files in the Camera's staging folder that the Relay opened during
        this Recording, oldest first."""
        folder = self._store.staging / recording.camera.id
        if not folder.is_dir():
            return []
        return sorted(p for p in folder.glob("*.mp4") if p.is_file()
                      and (opened := segment_started(p)) is not None and opened >= recording.started)

    def _interrupt(self, recording: Recording) -> None:
        """Place the files a cut-off Recording left in staging, oldest first:
        its open segment (known size) and any whose create hook never arrived."""
        recording.state = State.INTERRUPTED
        last_write = None
        for path in self._staged(recording):
            if recording.segment is not None and recording.segment.path == path:
                segment = recording.segment
            else:
                recording.parts += 1
                segment = Segment(path=path, width=0, height=0)
            last_write = path.stat().st_mtime
            recording.captures.append(self._store.place(path, self._name(recording, segment)))
        recording.segment = None
        recording.ended = (datetime.fromtimestamp(last_write).astimezone() if last_write is not None
                           else self._clock())
        log.warning("recording %s of %s was interrupted by a restart; placed %s",
                    recording.id, recording.camera.id, recording.captures)

    def _place_orphans(self) -> None:
        """Place every file left in staging that no Recording claimed: a
        segment whose hooks never reached the backend. Its name comes from
        its folder (``<device>/<camera name>``), the time the Relay opened it
        and the resolution inside the file. An empty file is only removed."""
        staging = self._store.staging
        for path in sorted(p for p in staging.rglob("*.mp4") if p.is_file()) if staging.is_dir() else []:
            parts = path.relative_to(staging).parts
            if len(parts) < 3:
                continue
            info = path.stat()
            if info.st_size == 0:
                path.unlink(missing_ok=True)
                continue
            when = segment_started(path) or datetime.fromtimestamp(info.st_mtime).astimezone()
            width, height = mp4_video_size(path) or (0, 0)
            name = self._store.place(path, CaptureName(device=parts[0], camera="/".join(parts[1:-1]),
                                                       when=when, width=width, height=height, ext="mp4"))
            log.warning("placed %s, left in staging with no Recording, as %s", path, name)

    def _name(self, recording: Recording, segment: Segment) -> CaptureName:
        width, height = segment.width, segment.height
        if not (width and height):
            width, height = (mp4_video_size(segment.path)
                             or (recording.camera.width or 0, recording.camera.height or 0))
        return CaptureName(device=recording.camera.device, camera=recording.camera.name,
                           when=recording.started, width=width, height=height,
                           ext=segment.path.suffix.lstrip(".") or "mp4", part=recording.parts)

    def _finalize(self, recording: Recording) -> None:
        recording.state = State.FINALIZED
        recording.ended = self._clock()
        log.info("recording %s of %s finalized (%s): %s", recording.id, recording.camera.id,
                 recording.reason, recording.captures)
        self._changed()

    def _changed(self) -> None:
        if self._journal is not None:  # before anyone hears of it, like the Device Registry
            self._journal.save(r for r in self._recordings.values()
                               if not r.finished or r.state is State.INTERRUPTED)
        self._changes.set()
        for listener in self._listeners:
            listener()
