"""Seam A: a setting the instance can't use stops it before anything starts,
with the same "refusing to start" message as a missing password; a setting
left empty in ``.env`` means its default."""

import pytest

from harness.instance import run_image_once


@pytest.mark.parametrize("name,value", [
    ("FEED_DOWN_AFTER_SECONDS", "five"),
    ("FEED_RTP_LOSS_MAX_PERCENT", "-1"),
    ("CV_HTTP_PORT", "eighty"),
    ("CV_DISK_REFUSE_BYTES", "lots"),
    ("CV_FEED_SAMPLE_SECONDS", "often"),
    ("QUALITY_WINDOW_SECONDS", "ten"),
    ("QUALITY_DOWN_LOSSY_WINDOWS", "4"),  # more than the 3 windows Auto looks back over
    ("CV_QUALITY_CONFIG_CHECK_SECONDS", "hourly"),
    ("CV_QUALITY_CONFIG_CHECK_SECONDS", "0"),
])
def test_an_unusable_setting_refuses_to_start_naming_it(instance_image, device_harness, name, value):
    result = run_image_once(instance_image, device_harness.session_id,
                            env={"CV_PASSWORD": "pw", name: value})
    output = result.stdout + result.stderr

    assert result.returncode == 2, output
    assert "refusing to start" in output
    assert name in output
    assert "Traceback" not in output


def test_settings_left_empty_keep_their_defaults(instances):
    empty = {name: "" for name in ("FEED_DOWN_AFTER_SECONDS", "FEED_ERRORED_FRAMES_MAX", "CV_DISK_REFUSE_BYTES",
                                   "CV_FEED_SAMPLE_SECONDS", "CV_RECORDING_PRESETS", "FEED_STALL_SECONDS")}
    started = instances.start("empty-settings", env=empty)

    with started.client() as api:
        assert api.get("/api/recordings/presets").json() == {"presets": [30, 60, 300]}


def test_a_leftover_bitrate_setting_does_not_stop_the_instance(instances):
    """The Degraded bitrate rule is gone (ADR 0008). An old `.env` may still set its variables,
    even to a value the rule would have refused, and the instance starts regardless."""
    leftover = {"FEED_BITRATE_AVERAGE_SECONDS": "60", "FEED_BITRATE_FLOOR_PERCENT": "-1"}
    started = instances.start("leftover-bitrate", env=leftover)

    with started.client() as api:
        assert api.get("/api/recordings/presets").json() == {"presets": [30, 60, 300]}
