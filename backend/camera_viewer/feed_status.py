"""Feed Status Evaluator: turns a time series of Relay samples for one Feed into its Feed Status.

A pure function with no I/O. The Feed Monitor collects the samples (from the Relay's path and
static-source endpoints) and Device reachability, and calls `evaluate_feed` on every new sample.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from itertools import pairwise


class FeedStatus(Enum):
    LIVE = "live"
    DEGRADED = "degraded"
    DOWN = "down"
    IDLE = "idle"
    OFFLINE = "offline"


@dataclass(frozen=True)
class RelaySample:
    """One reading of the Relay's counters for a Feed. Counters are cumulative, as MediaMTX reports them.

    `at` is a monotonic timestamp in seconds. `ready` is the path's `available` flag (not `online`,
    which stays true after a pulled source stops); `source_error` is the static source's
    `state == "error"`. RTP counters are None when the source has no RTP hop.
    """

    at: float
    ready: bool
    inbound_bytes: int
    source_error: bool = False
    frames_in_error: int = 0
    rtp_packets: int | None = None
    rtp_packets_lost: int | None = None

    @property
    def failing(self) -> bool:
        return not self.ready or self.source_error


@dataclass(frozen=True)
class Thresholds:
    """The tunable limits behind Feed Status. Each field maps to one ``FEED_<FIELD>`` environment
    variable (``config.feed_thresholds``)."""

    down_after_seconds: float = 5.0
    degraded_window_seconds: float = 10.0
    rtp_loss_max_percent: float = 1.0
    errored_frames_max: int = 0


def evaluate_feed(
    samples: Sequence[RelaySample],
    *,
    device_reachable: bool,
    pulled: bool,
    thresholds: Thresholds = Thresholds(),
) -> FeedStatus:
    """The Feed Status for a Feed, given its Relay samples in time order (oldest first).

    Precedence: Offline (Device unreachable), Idle (nobody pulling), Down (source failing for
    `down_after_seconds`: not ready, in error, or ready with no new bytes), Idle again while the Feed is still starting (no sample has shown the
    source ready yet), Degraded (failing for less than the Down threshold, or any Degraded rule
    firing within the last `degraded_window_seconds`), then Live.

    `samples` cover one pull: the caller starts a fresh series each time the Feed gains a puller,
    so a Feed that is just being opened reads as starting, not as failing.
    """
    if not device_reachable:
        return FeedStatus.OFFLINE
    if not pulled:
        return FeedStatus.IDLE
    if samples and _failing(samples, len(samples) - 1):
        if _failing_for(samples) >= thresholds.down_after_seconds:
            return FeedStatus.DOWN
    if not any(sample.ready and not sample.source_error for sample in samples):
        return FeedStatus.IDLE  # still starting: nothing received yet is no reason for an alarm
    if _failing(samples, len(samples) - 1):
        return FeedStatus.DEGRADED
    if _degraded(samples, thresholds):
        return FeedStatus.DEGRADED
    return FeedStatus.LIVE


def _degraded(samples: Sequence[RelaySample], t: Thresholds) -> bool:
    """Errored frames or RTP loss within the window. Falling bitrate is no signal: no kit encoder
    runs at a constant bitrate, so a still scene undershoots its target (ADR 0008)."""
    window = t.degraded_window_seconds
    if _increase_within(samples, window, lambda s: s.frames_in_error) > t.errored_frames_max:
        return True
    return lossy(samples, t, window)


def lossy(samples: Sequence[RelaySample], thresholds: Thresholds, window: float = math.inf) -> bool:
    """Whether RTP loss within the window (by default, across all of ``samples``) passes the
    Degraded limit. Auto reads loss through this too, so a window it steps down for is one that
    reads Degraded, and a deliberate step down never does (ADR 0009)."""
    return _rtp_loss_within(samples, window) * 100 > thresholds.rtp_loss_max_percent


def _failing(samples: Sequence[RelaySample], i: int) -> bool:
    """Whether the source was failing at sample ``i``: not ready, in error, or ready but silent,
    with no byte received since the sample before. A frozen Device keeps its path ready until
    the Relay's own read timeout (10 s on 1.21.2); silence shows it within one sample."""
    sample = samples[i]
    return sample.failing or (i > 0 and sample.ready and sample.inbound_bytes == samples[i - 1].inbound_bytes)


def _failing_for(samples: Sequence[RelaySample]) -> float:
    """Seconds the source has been failing without a break, measured from the first failing
    sample seen; silence from the last sample that still showed bytes arriving."""
    first = len(samples) - 1
    while first > 0 and _failing(samples, first - 1):
        first -= 1
    start = samples[first]
    if not start.failing and first > 0:  # silent: nothing has arrived since the sample before
        start = samples[first - 1]
    return samples[-1].at - start.at


def _rtp_loss_within(samples: Sequence[RelaySample], window: float) -> float:
    """Share of RTP packets lost within the window; 0 when the source has no RTP hop to count.

    MediaMTX 1.21.2 counts received and lost packets separately (gortsplib v5.6.7
    `pkg/rtpreceiver`: `received += len(pkts)`, `lost += gap`), so `inboundRTPPackets`
    excludes the lost ones and the packets expected are their sum.
    """
    rtp = [s for s in samples if s.rtp_packets is not None and s.rtp_packets_lost is not None]
    if len(rtp) < 2:
        return 0.0
    received = _increase_within(rtp, window, lambda s: s.rtp_packets)
    lost = _increase_within(rtp, window, lambda s: s.rtp_packets_lost)
    expected = received + lost
    return lost / expected if expected else 0.0


def _increase_within(samples: Sequence[RelaySample], window: float, counter: Callable[[RelaySample], int]) -> int:
    """How much a cumulative counter rose in samples taken less than `window` seconds before the last one.

    A counter that goes backwards was reset (the Relay re-created the path or source), so the
    new value is all increase since the reset.
    """
    now = samples[-1].at
    total = 0
    for before, after in pairwise(samples):
        if now - after.at >= window:
            continue
        delta = counter(after) - counter(before)
        total += delta if delta >= 0 else counter(after)
    return total
