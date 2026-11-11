"""Recording timers: end each timed Recording when its time is up.

A timed Recording runs for one of the instance's preset durations
(``CV_RECORDING_PRESETS``, 30 s, 1 min and 5 min by default). Its timer
counts wall-clock time from the moment it started, not recorded video, so a
Feed drop eats into the duration rather than extending it.

``RecordingTimers`` watches the Recorder like every other module does: it
arms a timer for each unfinished timed Recording it sees, and disarms it when
the Recording is stopped some other way first (by a user, Device removal or
low disk). When a timer runs out it stops the Recording with
``EndReason.TIMER``, which finalizes it like any other stop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from camera_viewer.recorder import EndReason, Recorder, Recording, State

log = logging.getLogger("camera_viewer.timers")


class RecordingTimers:
    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder
        self._armed: dict[str, asyncio.Task[None]] = {}  # recording id -> its timer
        recorder.subscribe(self._sync)

    def _sync(self) -> None:
        due = {r.id: r for r in self._recorder.unfinished()
               if r.ends is not None and r.state is not State.STOPPING}
        for recording_id in self._armed.keys() - due.keys():
            self._armed.pop(recording_id).cancel()  # ended some other way first
        for recording_id in due.keys() - self._armed.keys():
            self._arm(due[recording_id])

    def _arm(self, recording: Recording) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # the Recorder only changes inside the app's event loop
            log.error("recording %s: no event loop to run its timer on", recording.id)
            return
        self._armed[recording.id] = loop.create_task(self._run(recording),
                                                     name=f"timer-{recording.id}")

    async def _run(self, recording: Recording) -> None:
        assert recording.ends is not None
        await asyncio.sleep(max((recording.ends - datetime.now(recording.ends.tzinfo)).total_seconds(), 0))
        # Disarm before stopping: the stop changes the Recording, and this
        # task must not be cancelled by the sync that change triggers.
        self._armed.pop(recording.id, None)
        log.info("recording %s of %s: %s s are up", recording.id, recording.camera.id, recording.duration)
        await self._recorder.stop(recording.id, EndReason.TIMER)
