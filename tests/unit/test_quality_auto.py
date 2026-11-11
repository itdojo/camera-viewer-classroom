"""Auto controller, tested directly as a pure function from synthetic sample series (ADR 0009, #49),
as the Feed Status evaluator is in ``test_feed_status.py``.

``drive`` plays a simulated link one sample a second: 100 RTP packets a second, of which 5 go
missing (5%, over the 1% Degraded limit) in every second the link calls lossy. A link can be
lossy only at some levels, so a step changes what the next samples show, as on a real Device.
"""

from itertools import pairwise

import pytest

from camera_viewer.config import quality_thresholds
from camera_viewer.feed_status import RelaySample
from camera_viewer.quality import CSI_LADDER, USB_LADDER, Level
from camera_viewer.quality_auto import AutoContext, AutoState, AutoThresholds, decide

HIGH, MEDIUM, LOW, MINIMUM = Level.HIGH, Level.MEDIUM, Level.LOW, Level.MINIMUM


def drive(seconds, *, level, ladder=CSI_LADDER, lossy=lambda t, level: False, ready=lambda t: True,
          recording=lambda t: False, thresholds=AutoThresholds()):
    """Run the controller over ``seconds`` of a simulated pull starting at the given level. Returns
    every step as ``(t, level)``. Samples are trimmed to the last 40 s, as the Feed Monitor keeps
    only a short history."""
    samples, steps, state = [], [], AutoState()
    received = lost = inbound = 0
    for t in range(seconds + 1):
        if t and ready(t):
            missing = 5 if lossy(t, level) else 0
            received, lost, inbound = received + 100 - missing, lost + missing, inbound + 10_000
        samples.append(RelaySample(at=t, ready=ready(t), inbound_bytes=inbound, rtp_packets=received,
                                   rtp_packets_lost=lost))
        samples = [s for s in samples if t - s.at <= 40]
        state, step = decide(samples, state, AutoContext(level, ladder, recording(t)), thresholds=thresholds)
        if step is not None:
            steps.append((t, step.level))
            level = step.level
    return steps


def always(t, level):
    return True


def lossy_between(start, end):
    return lambda t, level: start <= t < end


def lossy_at(*levels):
    """A link that can't carry these levels and carries every lower one cleanly."""
    return lambda t, level: level in levels


# -- stepping down -------------------------------------------------------------------------------


def test_a_feed_lossy_in_two_of_three_windows_steps_down_one_level():
    # Windows of 10 s from the pull's start: 0-10 and 10-20 are both lossy.
    assert drive(20, level=HIGH, lossy=always)[:1] == [(20, MEDIUM)]


def test_one_lossy_window_is_not_enough():
    assert drive(120, level=HIGH, lossy=lossy_between(12, 19)) == []


def test_two_lossy_windows_out_of_three_need_not_be_adjacent():
    lossy = lambda t, level: 1 <= t <= 10 or 21 <= t <= 30  # noqa: E731 - windows 1 and 3, not 2
    assert drive(30, level=HIGH, lossy=lossy) == [(30, MEDIUM)]


def test_a_link_that_stays_bad_is_stepped_down_one_level_at_a_time_to_minimum_and_no_further():
    steps = drive(200, level=HIGH, lossy=always)
    assert [level for _, level in steps] == [MEDIUM, LOW, MINIMUM]
    assert steps[0] == (20, MEDIUM)
    # Each level is judged afresh: two more lossy windows after every step.
    assert [b - a for (a, _), (b, _) in pairwise(steps)] == [20, 20]


def test_a_step_down_that_cures_the_loss_holds_although_the_backlog_still_drains_for_a_window():
    # The link can't carry High. After the step, the Device's queue still drains loss for 8 s.
    stepped = []

    def lossy(t, level):
        return level is HIGH or (stepped and t < stepped[0] + 8)

    def watch(t, level):
        if level is MEDIUM and not stepped:
            stepped.append(t)
        return lossy(t, level)

    assert drive(55, level=HIGH, lossy=watch) == [(20, MEDIUM)]


def down_from(t_down):
    return lambda t: t < t_down


def test_a_feed_that_goes_down_during_a_pull_steps_down_at_once():
    # Not ready from 21 s: Down 5 s later (FEED_DOWN_AFTER_SECONDS), well before two windows end.
    steps = drive(40, level=HIGH, ready=down_from(21))
    assert steps[0] == (26, MEDIUM)


def test_a_feed_still_down_after_its_step_is_not_stepped_again_until_it_has_received():
    # The new level never comes up: a level that never played can't be blamed on the link.
    assert drive(120, level=HIGH, ready=down_from(21)) == [(26, MEDIUM)]


def test_a_feed_that_never_started_does_not_step():
    assert drive(60, level=HIGH, ready=lambda t: False) == []


# -- stepping up ---------------------------------------------------------------------------------


def test_after_sixty_clean_seconds_auto_tries_one_level_up():
    # CSI Medium to High keeps the resolution, so it drops no viewers.
    assert drive(70, level=MEDIUM)[:1] == [(60, HIGH)]


def test_a_step_up_that_holds_its_trial_stays_and_the_next_waits_sixty_seconds_again():
    steps = drive(130, level=MINIMUM, ladder=CSI_LADDER, lossy=lossy_at())
    assert steps[:1] == [(60, LOW)]
    # Low to Medium crosses 720p to 1080p, so it waits the disruptive 5 minutes, not 60 s.
    assert drive(400, level=MINIMUM, ladder=CSI_LADDER)[:2] == [(60, LOW), (360, MEDIUM)]


def test_a_lossy_window_during_the_trial_sends_it_back_down_and_doubles_the_wait():
    steps = drive(250, level=MEDIUM, lossy=lossy_at(HIGH))
    # Up at 60; the first window on trial (60-70) is lossy, so back down at 70; then 120 s clean.
    assert steps[:3] == [(60, HIGH), (70, MEDIUM), (190, HIGH)]


def test_the_wait_doubles_after_each_failed_trial_up_to_sixteen_minutes():
    steps = drive(6000, level=MEDIUM, lossy=lossy_at(HIGH))
    ups = [t for t, level in steps if level is HIGH]
    downs = [t for t, level in steps if level is MEDIUM]
    waits = [up - down for down, up in zip(downs, ups[1:], strict=False)]
    assert waits[:6] == [120, 240, 480, 960, 960, 960]


def test_a_step_up_that_holds_resets_the_wait():
    # High is lossy until 300 s, so two trials fail and the wait reaches 240 s; the third holds.
    # From 600 s High is lossy again: down two windows later, and back up after 60 s, not 480.
    lossy = lambda t, level: level is HIGH and (t < 300 or t >= 600)  # noqa: E731
    assert drive(685, level=MEDIUM, lossy=lossy) == [
        (60, HIGH), (70, MEDIUM), (190, HIGH), (200, MEDIUM), (440, HIGH), (620, MEDIUM), (680, HIGH)]


def test_a_step_up_that_drops_viewers_waits_five_clean_minutes_first():
    # Every step on the ffmpeg kits restarts ffmpeg.
    assert drive(400, level=MEDIUM, ladder=USB_LADDER)[:1] == [(300, HIGH)]


def test_loss_restarts_the_clean_count():
    assert drive(100, level=MEDIUM, lossy=lossy_between(31, 35))[:1] == [(100, HIGH)]


def test_a_feed_that_is_not_receiving_is_not_clean():
    ready = lambda t: not 40 <= t <= 42  # noqa: E731 - a short drop, too short for Down
    assert drive(110, level=MEDIUM, ready=ready)[:1] == [(102, HIGH)]


def test_auto_never_goes_above_high():
    assert drive(2000, level=HIGH) == []


# -- while Recording ------------------------------------------------------------------------------


def test_while_recording_auto_never_steps_up_across_a_step_that_drops_viewers():
    assert drive(1000, level=MEDIUM, ladder=USB_LADDER, recording=lambda t: True) == []
    assert drive(1000, level=LOW, ladder=CSI_LADDER, recording=lambda t: True) == []


def test_while_recording_a_step_up_that_keeps_viewers_still_happens():
    assert drive(70, level=MEDIUM, ladder=CSI_LADDER, recording=lambda t: True)[:1] == [(60, HIGH)]


def test_while_recording_auto_still_steps_down():
    assert drive(20, level=HIGH, ladder=USB_LADDER, lossy=always, recording=lambda t: True)[:1] == [(20, MEDIUM)]


def test_once_the_recording_ends_the_step_up_follows():
    assert drive(400, level=MEDIUM, ladder=USB_LADDER, recording=lambda t: t < 350)[:1] == [(350, HIGH)]


# -- thresholds from the environment ----------------------------------------------------------------


def test_the_defaults_are_adr_0009s():
    assert AutoThresholds() == quality_thresholds({})
    t = AutoThresholds()
    assert (t.window_seconds, t.down_lossy_windows, t.down_lookback_windows, t.up_clean_seconds,
            t.up_trial_seconds, t.up_wait_max_seconds, t.up_disruptive_clean_seconds) == (10, 2, 3, 60, 30, 960, 300)


def test_every_timing_can_be_overridden_from_quality_variables():
    env = {"QUALITY_WINDOW_SECONDS": "5", "QUALITY_DOWN_LOSSY_WINDOWS": "1", "QUALITY_DOWN_LOOKBACK_WINDOWS": "2",
           "QUALITY_UP_CLEAN_SECONDS": "20", "QUALITY_UP_TRIAL_SECONDS": "15", "QUALITY_UP_WAIT_MAX_SECONDS": "80",
           "QUALITY_UP_DISRUPTIVE_CLEAN_SECONDS": "25"}
    assert quality_thresholds(env) == AutoThresholds(5, 1, 2, 20, 15, 80, 25)


def test_shorter_timings_change_what_auto_does():
    fast = quality_thresholds({"QUALITY_WINDOW_SECONDS": "5", "QUALITY_UP_CLEAN_SECONDS": "20",
                                    "QUALITY_UP_DISRUPTIVE_CLEAN_SECONDS": "30"})
    assert drive(10, level=HIGH, lossy=always, thresholds=fast)[:1] == [(10, MEDIUM)]
    assert drive(40, level=MEDIUM, ladder=USB_LADDER, thresholds=fast)[:1] == [(30, HIGH)]


@pytest.mark.parametrize(("name", "value"), [
    ("QUALITY_WINDOW_SECONDS", "ten"),
    ("QUALITY_WINDOW_SECONDS", "0"),
    ("QUALITY_DOWN_LOSSY_WINDOWS", "1.5"),
    ("QUALITY_DOWN_LOSSY_WINDOWS", "0"),
    ("QUALITY_DOWN_LOOKBACK_WINDOWS", "-3"),
    ("QUALITY_UP_CLEAN_SECONDS", "0"),
    ("QUALITY_UP_TRIAL_SECONDS", "-1"),
    ("QUALITY_UP_WAIT_MAX_SECONDS", "soon"),
    ("QUALITY_UP_DISRUPTIVE_CLEAN_SECONDS", "0"),
])
def test_an_unreadable_timing_is_refused_naming_the_variable(name, value):
    with pytest.raises(ValueError, match=name):
        quality_thresholds({name: value})


def test_more_lossy_windows_than_the_lookback_holds_is_refused():
    with pytest.raises(ValueError, match="QUALITY_DOWN_LOSSY_WINDOWS"):
        quality_thresholds({"QUALITY_DOWN_LOSSY_WINDOWS": "4"})


def test_a_wait_cap_below_the_clean_wait_is_refused():
    with pytest.raises(ValueError, match="QUALITY_UP_WAIT_MAX_SECONDS"):
        quality_thresholds({"QUALITY_UP_WAIT_MAX_SECONDS": "30"})


def test_an_empty_quality_variable_keeps_its_default():
    assert quality_thresholds({"QUALITY_WINDOW_SECONDS": " "}) == AutoThresholds()
