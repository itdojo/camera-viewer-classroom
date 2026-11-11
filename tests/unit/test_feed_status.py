"""Feed Status Evaluator, tested directly as a pure function (spec #1, Testing Decisions).

Sample series are synthetic: one RelaySample per second unless a test says otherwise.
"""

from dataclasses import replace

import pytest

from camera_viewer.config import feed_thresholds
from camera_viewer.feed_status import FeedStatus, RelaySample, Thresholds, evaluate_feed


def receiving(seconds: int, bytes_per_second: int = 100_000, start: float = 0.0) -> list[RelaySample]:
    """A healthy Feed: ready, no errors, steady bitrate, one sample per second."""
    return [
        RelaySample(at=start + i, ready=True, inbound_bytes=i * bytes_per_second)
        for i in range(seconds + 1)
    ]


def test_a_steadily_receiving_feed_is_live():
    assert evaluate_feed(receiving(30), device_reachable=True, pulled=True) is FeedStatus.LIVE


def test_an_unreachable_device_makes_the_feed_offline_even_while_it_still_looks_live():
    assert evaluate_feed(receiving(30), device_reachable=False, pulled=True) is FeedStatus.OFFLINE


def not_ready(seconds: int, start: float = 0.0, inbound_bytes: int = 0) -> list[RelaySample]:
    """A Feed whose source is not ready, one sample per second from `start` to `start + seconds`."""
    return [RelaySample(at=start + i, ready=False, inbound_bytes=inbound_bytes) for i in range(seconds + 1)]


def test_a_feed_nobody_pulls_is_idle_not_down():
    assert evaluate_feed(not_ready(60), device_reachable=True, pulled=False) is FeedStatus.IDLE


def test_offline_takes_precedence_over_idle():
    assert evaluate_feed(not_ready(60), device_reachable=False, pulled=False) is FeedStatus.OFFLINE


def outage(last_good_at: float, bad_from: float, now: float, *, ready: bool = False, source_error: bool = False):
    """Live up to `last_good_at`, then a source that is not ready or in error from `bad_from` through `now`."""
    good = receiving(int(last_good_at))
    total = good[-1].inbound_bytes
    bad_times = [bad_from + i for i in range(int(now - bad_from) + 1)]
    if bad_times[-1] != now:
        bad_times.append(now)
    bad = [RelaySample(at=t, ready=ready, source_error=source_error, inbound_bytes=total) for t in bad_times]
    return good + bad


def test_a_source_not_ready_for_five_seconds_is_down():
    samples = outage(last_good_at=20, bad_from=21, now=26)
    assert evaluate_feed(samples, device_reachable=True, pulled=True) is FeedStatus.DOWN


def test_a_source_not_ready_for_just_under_five_seconds_is_not_down():
    samples = outage(last_good_at=20, bad_from=21, now=25.9)
    assert evaluate_feed(samples, device_reachable=True, pulled=True) is not FeedStatus.DOWN


def test_a_source_in_error_for_five_seconds_is_down_even_if_the_path_still_reports_ready():
    samples = outage(last_good_at=20, bad_from=21, now=26, ready=True, source_error=True)
    assert evaluate_feed(samples, device_reachable=True, pulled=True) is FeedStatus.DOWN


def test_a_source_in_error_for_just_under_five_seconds_is_not_down():
    samples = outage(last_good_at=20, bad_from=21, now=25.9, ready=True, source_error=True)
    assert evaluate_feed(samples, device_reachable=True, pulled=True) is not FeedStatus.DOWN


def test_a_feed_that_never_became_ready_is_down_after_five_seconds():
    assert evaluate_feed(not_ready(5), device_reachable=True, pulled=True) is FeedStatus.DOWN


def test_idle_and_offline_take_precedence_over_down():
    samples = outage(last_good_at=20, bad_from=21, now=40)
    assert evaluate_feed(samples, device_reachable=True, pulled=False) is FeedStatus.IDLE
    assert evaluate_feed(samples, device_reachable=False, pulled=True) is FeedStatus.OFFLINE


def test_an_outage_shorter_than_the_down_threshold_is_degraded_because_nothing_is_being_received():
    samples = outage(last_good_at=20, bad_from=21, now=25.9)
    assert evaluate_feed(samples, device_reachable=True, pulled=True) is FeedStatus.DEGRADED


def test_a_feed_still_starting_up_stays_idle_until_its_source_is_first_ready():
    # Opening an on-demand Feed takes a second or two; that is not a hiccup worth an alarm.
    assert evaluate_feed(not_ready(2), device_reachable=True, pulled=True) is FeedStatus.IDLE
    assert evaluate_feed([], device_reachable=True, pulled=True) is FeedStatus.IDLE


def test_a_feed_that_has_not_started_within_the_down_threshold_is_down():
    starting = [RelaySample(at=t, ready=False, inbound_bytes=0) for t in (0, 1, 2, 3, 4, 4.9)]
    assert evaluate_feed(starting, device_reachable=True, pulled=True) is FeedStatus.IDLE
    assert evaluate_feed([*starting, RelaySample(at=5, ready=False, inbound_bytes=0)],
                         device_reachable=True, pulled=True) is FeedStatus.DOWN


def test_a_feed_that_was_ready_once_and_then_fails_briefly_is_degraded_not_idle():
    samples = receiving(1) + not_ready(2, start=2, inbound_bytes=100_000)
    assert evaluate_feed(samples, device_reachable=True, pulled=True) is FeedStatus.DEGRADED


def with_errored_frames(samples: list[RelaySample], first_seen_at: float, count: int = 1) -> list[RelaySample]:
    """Raise the cumulative errored-frame counter by `count` at the sample taken at `first_seen_at`."""
    return [replace(s, frames_in_error=count if s.at >= first_seen_at else 0) for s in samples]


def test_errored_frames_seen_within_the_last_ten_seconds_degrade_the_feed():
    samples = with_errored_frames(receiving(40), first_seen_at=31)  # 9 s before the last sample
    assert evaluate_feed(samples, device_reachable=True, pulled=True) is FeedStatus.DEGRADED


def test_errored_frames_seen_ten_seconds_ago_have_aged_out():
    samples = with_errored_frames(receiving(40), first_seen_at=30)  # exactly 10 s before the last sample
    assert evaluate_feed(samples, device_reachable=True, pulled=True) is FeedStatus.LIVE


def with_rtp(samples: list[RelaySample], *, lost_at: float = 0.0, lost: int = 0, packets_per_second: int = 100):
    """Steady RTP counters on an RTSP source; `lost` of the expected packets go missing at `lost_at`.

    Over the last 10 one-second intervals 1000 packets are expected, so `lost=10` is exactly 1% loss.
    """
    out = []
    for s in samples:
        missing = lost if s.at >= lost_at and lost else 0
        out.append(replace(s, rtp_packets=int(s.at) * packets_per_second - missing, rtp_packets_lost=missing))
    return out


def test_rtp_loss_above_one_percent_within_the_last_ten_seconds_degrades_the_feed():
    samples = with_rtp(receiving(40), lost_at=35, lost=11)
    assert evaluate_feed(samples, device_reachable=True, pulled=True) is FeedStatus.DEGRADED


def test_rtp_loss_of_exactly_one_percent_does_not_degrade_the_feed():
    samples = with_rtp(receiving(40), lost_at=35, lost=10)
    assert evaluate_feed(samples, device_reachable=True, pulled=True) is FeedStatus.LIVE


def test_rtp_loss_older_than_ten_seconds_has_aged_out():
    samples = with_rtp(receiving(40), lost_at=30, lost=500)
    assert evaluate_feed(samples, device_reachable=True, pulled=True) is FeedStatus.LIVE


def test_a_source_without_rtp_counters_is_judged_without_the_loss_rule():
    assert evaluate_feed(receiving(40), device_reachable=True, pulled=True) is FeedStatus.LIVE


def motion_then_still(*, motion_bytes_per_second: int, still_bytes_per_second: int,
                      still_from: int, total_seconds: int) -> list[RelaySample]:
    """A Feed whose scene stops moving at `still_from`: the encoder (VBR on every kit) then sends far
    less than its target, with no RTP loss and no errored frames."""
    samples, total = [RelaySample(at=0, ready=True, inbound_bytes=0)], 0
    for t in range(1, total_seconds + 1):
        total += still_bytes_per_second if t > still_from else motion_bytes_per_second
        samples.append(RelaySample(at=t, ready=True, inbound_bytes=total))
    return with_rtp(samples)


def test_a_feed_whose_source_goes_from_motion_to_a_still_scene_stays_live():
    # 500 kB/s while there is motion, then a still scene at 2% of that: under the old bitrate rule
    # this read Degraded for about a minute.
    samples = motion_then_still(motion_bytes_per_second=500_000, still_bytes_per_second=10_000,
                                still_from=60, total_seconds=120)
    for now in range(60, 121):
        assert evaluate_feed(samples[: now + 1], device_reachable=True, pulled=True) is FeedStatus.LIVE, now


def test_a_feed_losing_packets_as_its_bitrate_falls_reads_degraded_from_the_loss():
    # A Device uplink that can't keep up: bytes fall and the Device drops packets from its send queue,
    # which the Relay counts as RTP loss (1.1% here, over the 1% limit).
    still = motion_then_still(motion_bytes_per_second=500_000, still_bytes_per_second=100_000,
                              still_from=30, total_seconds=40)
    assert evaluate_feed(still, device_reachable=True, pulled=True) is FeedStatus.LIVE
    lossy = with_rtp(still, lost_at=35, lost=11)
    assert evaluate_feed(lossy, device_reachable=True, pulled=True) is FeedStatus.DEGRADED


# --- Transitions ---------------------------------------------------------------------------------


def test_a_feed_moves_through_an_outage_and_back_to_live():
    # 10000 B/s until t=60, nothing received while not ready from t=61 to t=70, then 10000 B/s again.
    samples, total = [], 0
    for t in range(0, 101):
        ready = not 61 <= t <= 70
        if t and ready:
            total += 10_000
        samples.append(RelaySample(at=t, ready=ready, inbound_bytes=total))

    def status_at(t: int) -> FeedStatus:
        return evaluate_feed(samples[: t + 1], device_reachable=True, pulled=True)

    assert status_at(60) is FeedStatus.LIVE
    assert status_at(63) is FeedStatus.DEGRADED  # out for 2 s: not yet Down
    assert status_at(65) is FeedStatus.DEGRADED  # out for 4 s
    assert status_at(66) is FeedStatus.DOWN  # out for 5 s
    assert status_at(70) is FeedStatus.DOWN
    assert status_at(71) is FeedStatus.LIVE  # receiving again, with no loss or errored frames to count


def test_a_feed_returns_to_live_once_errored_frames_leave_the_window():
    samples = with_errored_frames(receiving(60), first_seen_at=30)
    assert evaluate_feed(samples[:31], device_reachable=True, pulled=True) is FeedStatus.DEGRADED
    assert evaluate_feed(samples[:40], device_reachable=True, pulled=True) is FeedStatus.DEGRADED
    assert evaluate_feed(samples[:41], device_reachable=True, pulled=True) is FeedStatus.LIVE


def test_a_feed_that_loses_its_viewers_goes_idle_and_comes_back_live_when_pulled_again():
    samples = receiving(30)
    assert evaluate_feed(samples, device_reachable=True, pulled=True) is FeedStatus.LIVE
    assert evaluate_feed(samples, device_reachable=True, pulled=False) is FeedStatus.IDLE
    assert evaluate_feed(samples, device_reachable=False, pulled=False) is FeedStatus.OFFLINE
    assert evaluate_feed(samples, device_reachable=True, pulled=True) is FeedStatus.LIVE


def test_a_byte_counter_reset_by_the_relay_is_not_mistaken_for_a_bitrate_collapse():
    # The Relay re-creates the source at t=61 and its counter restarts from zero.
    samples = receiving(60, bytes_per_second=10_000) + [
        RelaySample(at=60 + i, ready=True, inbound_bytes=i * 10_000) for i in range(1, 6)
    ]
    assert evaluate_feed(samples, device_reachable=True, pulled=True) is FeedStatus.LIVE


# --- Thresholds from the environment -------------------------------------------------------------


def evaluate_with_env(samples, env: dict[str, str]) -> FeedStatus:
    return evaluate_feed(samples, device_reachable=True, pulled=True, thresholds=feed_thresholds(env))


def test_the_down_threshold_can_be_overridden():
    samples = outage(last_good_at=20, bad_from=21, now=23)
    assert evaluate_with_env(samples, {}) is FeedStatus.DEGRADED
    assert evaluate_with_env(samples, {"FEED_DOWN_AFTER_SECONDS": "2"}) is FeedStatus.DOWN


def test_the_degraded_window_can_be_overridden():
    samples = with_errored_frames(receiving(40), first_seen_at=25)  # 15 s before the last sample
    assert evaluate_with_env(samples, {}) is FeedStatus.LIVE
    assert evaluate_with_env(samples, {"FEED_DEGRADED_WINDOW_SECONDS": "20"}) is FeedStatus.DEGRADED


def test_the_rtp_loss_limit_can_be_overridden():
    samples = with_rtp(receiving(40), lost_at=35, lost=11)  # 1.1% loss
    assert evaluate_with_env(samples, {}) is FeedStatus.DEGRADED
    assert evaluate_with_env(samples, {"FEED_RTP_LOSS_MAX_PERCENT": "2"}) is FeedStatus.LIVE


def test_the_errored_frame_allowance_can_be_overridden():
    samples = with_errored_frames(receiving(40), first_seen_at=35, count=3)
    assert evaluate_with_env(samples, {}) is FeedStatus.DEGRADED
    assert evaluate_with_env(samples, {"FEED_ERRORED_FRAMES_MAX": "3"}) is FeedStatus.LIVE
    assert evaluate_with_env(samples, {"FEED_ERRORED_FRAMES_MAX": "2"}) is FeedStatus.DEGRADED


def test_a_malformed_threshold_is_rejected_naming_the_variable():
    with pytest.raises(ValueError, match="FEED_DOWN_AFTER_SECONDS"):
        feed_thresholds({"FEED_DOWN_AFTER_SECONDS": "five"})
    with pytest.raises(ValueError, match="FEED_RTP_LOSS_MAX_PERCENT"):
        feed_thresholds({"FEED_RTP_LOSS_MAX_PERCENT": "-1"})


def test_a_leftover_bitrate_threshold_is_ignored():
    """The bitrate rule is gone (ADR 0008); an old `.env` that still sets it must not stop the instance."""
    leftover = {"FEED_BITRATE_AVERAGE_SECONDS": "60", "FEED_BITRATE_FLOOR_PERCENT": "-1"}
    assert feed_thresholds(leftover) == Thresholds()


def test_an_empty_threshold_variable_keeps_its_default():
    """A `.env` line like `FEED_DOWN_AFTER_SECONDS=` means "not set", as for the disk thresholds."""
    assert feed_thresholds({"FEED_DOWN_AFTER_SECONDS": "", "FEED_ERRORED_FRAMES_MAX": "  "}) == Thresholds()


def silent(last_good_at: float, now: float) -> list[RelaySample]:
    """Live up to `last_good_at`, then still reported ready but receiving nothing: a Device frozen
    mid-stream, which the Relay only notices at its own read timeout."""
    good = receiving(int(last_good_at))
    total = good[-1].inbound_bytes
    return good + [RelaySample(at=last_good_at + i, ready=True, inbound_bytes=total)
                   for i in range(1, int(now - last_good_at) + 1)]


def test_a_ready_source_that_receives_nothing_for_five_seconds_is_down():
    assert evaluate_feed(silent(last_good_at=20, now=25), device_reachable=True, pulled=True) is FeedStatus.DOWN


def test_a_ready_source_that_received_nothing_for_just_under_five_seconds_is_degraded_not_down():
    samples = silent(last_good_at=20, now=24)
    assert evaluate_feed(samples, device_reachable=True, pulled=True) is FeedStatus.DEGRADED


def test_one_sample_with_no_new_bytes_is_not_down():
    """A sample can land before the next packet does; one flat reading is no outage."""
    samples = receiving(30)
    samples[25] = replace(samples[25], inbound_bytes=samples[24].inbound_bytes)
    assert evaluate_feed(samples, device_reachable=True, pulled=True) is not FeedStatus.DOWN
