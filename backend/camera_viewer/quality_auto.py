"""Auto controller: from a Feed's Relay samples, whether Auto steps its Quality down, up, or holds.

A pure function with no I/O, the same shape as the Feed Status Evaluator (ADR 0009). The Feed
Monitor calls ``decide`` on every new sample of a pulled Camera on Auto, keeps the returned
``AutoState`` in memory, and sends any step to the Quality Service, the one way a Quality changes.

Auto reads loss, not Feed Status. Time is cut into windows of ``window_seconds`` from when the
current level began to be judged (a step, or the start of the pull), and a window is lossy when
its RTP loss would trip Degraded's loss rule (``feed_status.lossy``). So a congested Feed's brief
stall-driven Degraded (ADR 0008's one-flat-sample rule) never moves the Quality.

- **Down** one level when ``down_lossy_windows`` of the last ``down_lookback_windows`` windows at
  this level are lossy, or at once when the Feed goes Down after having received at this level:
  a Device write blocked for 10 s ends the session, so a collapsed link would otherwise sit for
  two windows. A level that never came up is no evidence about the link, so it never steps again.
  Windows restart at every step, so the backlog a Device still drains after a step down (about one
  window, #48) is a single lossy window and never a second step on its own.
- **Up** one level once the Feed has been clean (no lossy window, never failing) for the wait:
  ``up_clean_seconds``, or ``up_disruptive_clean_seconds`` when the step drops viewers. VBR
  encoders show no headroom, so the only way to learn the link carries more is to try it. The step
  is then on trial for ``up_trial_seconds``: a lossy window sends it back down and doubles the wait,
  up to ``up_wait_max_seconds``; a trial that holds resets the wait.
- **While Recording** Auto still steps down, since lost packets already corrupt the file, but never
  up across a step that drops viewers, which would split the Recording.

Never below Minimum or above High.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from camera_viewer.feed_status import FeedStatus, RelaySample, Thresholds, evaluate_feed, lossy
from camera_viewer.quality import Ladder, Level


@dataclass(frozen=True)
class AutoThresholds:
    """Auto's timings. Each field maps to one ``QUALITY_<FIELD>`` environment variable
    (``config.quality_thresholds``)."""

    window_seconds: float = 10.0
    down_lossy_windows: int = 2
    down_lookback_windows: int = 3
    up_clean_seconds: float = 60.0
    up_trial_seconds: float = 30.0
    up_wait_max_seconds: float = 960.0
    up_disruptive_clean_seconds: float = 300.0


@dataclass(frozen=True)
class AutoContext:
    """What Auto needs to know about one Camera besides its samples: its level, the ladder that
    level sits on, and whether it is Recording. The Quality Service gives it only while the Camera
    is Auto's to step (``QualityService.auto_context``)."""

    level: Level
    ladder: Ladder
    recording: bool


@dataclass(frozen=True)
class AutoState:
    """What Auto remembers about one Camera between samples. In memory only: after an instance
    restart Auto starts afresh from the stored level, which is all that needs to survive."""

    level: Level | None = None           # the level the timings below belong to
    since: float | None = None           # when judging this level began: a step, or the pull's start
    window_start: float | None = None
    windows: tuple[bool, ...] = ()       # whether each finished window was lossy, oldest first
    clean_since: float | None = None
    wait: float | None = None            # the wait before a step up; None is ``up_clean_seconds``
    trial_from: float | None = None      # a step up is on trial since then
    trial_wait: float | None = None      # the wait that step up waited out

    def restarted(self, now: float) -> AutoState:
        """Judge afresh from ``now``, as at the start of a pull or once a step's restart is over.
        The wait carries over, and so does a trial, which restarts its clock."""
        return replace(self, since=now, window_start=now, windows=(), clean_since=now,
                       trial_from=now if self.trial_from is not None else None)


@dataclass(frozen=True)
class Step:
    level: Level
    why: str


def decide(samples: Sequence[RelaySample], state: AutoState, context: AutoContext, *,
           thresholds: AutoThresholds = AutoThresholds(),
           feed: Thresholds = Thresholds()) -> tuple[AutoState, Step | None]:
    """Auto's next state, and the step to take now, if any, for a Camera at ``context.level``.

    ``samples`` are the current pull's, oldest first, ending with the newest, and must reach back
    at least one window. ``feed`` holds Degraded's loss limit and the Down threshold.
    """
    if not samples:
        return state, None
    level, ladder = context.level, context.ladder
    now = samples[-1].at
    if state.level != level or state.since is None:  # first look, or the level changed elsewhere
        state = AutoState(level=level, wait=state.wait).restarted(now)
    recent = [s for s in samples if s.at >= state.since]
    wait = state.wait if state.wait is not None else thresholds.up_clean_seconds

    if now - state.window_start >= thresholds.window_seconds:
        bad = lossy([s for s in recent if s.at >= state.window_start], feed)
        state = replace(state, window_start=now, windows=(*state.windows, bad)[-thresholds.down_lookback_windows:],
                        clean_since=now if bad else state.clean_since)
        if bad and state.trial_from is not None:
            return _down(state, level, ladder, now, "a lossy window on trial", wait=_doubled(state, thresholds))
    if samples[-1].failing:
        state = replace(state, clean_since=now)

    if (any(s.ready and not s.source_error for s in recent)
            and evaluate_feed(recent, device_reachable=True, pulled=True, thresholds=feed) is FeedStatus.DOWN):
        return _down(state, level, ladder, now, "Feed Down",
                     wait=_doubled(state, thresholds) if state.trial_from is not None else wait)
    if sum(state.windows) >= thresholds.down_lossy_windows:
        return _down(state, level, ladder, now,
                     f"loss in {sum(state.windows)} of the last {len(state.windows)} windows", wait=wait)

    if state.trial_from is not None:
        if now - state.trial_from >= thresholds.up_trial_seconds:
            state = replace(state, trial_from=None, trial_wait=None, wait=None)  # it held: the wait resets
        return state, None
    up = ladder.above(level)
    if up is None:
        return state, None
    disruptive = ladder.disrupts(level, up)
    if disruptive and context.recording:
        return state, None
    needed = max(wait, thresholds.up_disruptive_clean_seconds) if disruptive else wait
    if now - state.clean_since < needed:
        return state, None
    stepped = AutoState(level=up, wait=wait, trial_from=now, trial_wait=needed).restarted(now)
    return stepped, Step(up, f"clean for {needed:g} s")


def _doubled(state: AutoState, thresholds: AutoThresholds) -> float:
    """The wait after a step up that failed its trial: twice what it waited, up to the cap."""
    return min(2 * state.trial_wait, thresholds.up_wait_max_seconds)


def _down(state: AutoState, level: Level, ladder: Ladder, now: float, why: str,
          *, wait: float) -> tuple[AutoState, Step | None]:
    below = ladder.below(level)
    if below is None:  # already at Minimum: nowhere to go, but keep watching
        return state, None
    return AutoState(level=below, wait=wait).restarted(now), Step(below, why)

