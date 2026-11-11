"""Quality translation, tested directly as pure functions (spec #46, Testing Decisions).

A level plus a Camera's live path config becomes the PATCH body the Device
takes, and a live path config reads back to a level. Expected values come
from ADR 0009's table and the Device Kit's own ``mediamtx.yml`` strings.
"""

import re

import pytest

from camera_viewer.quality import LADDERS, Kit, Level, Setting, kit_of, live_setting, patch_for

USB_KIT = (
    "ffmpeg -hide_banner -loglevel warning "
    "-f v4l2 -input_format mjpeg -video_size 1280x720 -framerate 15 "
    "-i /dev/camera-usb "
    "-vf $USB_CAMERA_FILTER "
    "-c:v libx264 -preset ultrafast -tune zerolatency -bf 0 "
    "-pix_fmt yuv420p -g 15 -b:v 1M -maxrate 1M -bufsize 500k "
    "-f rtsp -rtsp_transport tcp rtsps://127.0.0.1:8322/usb"
)
# The kit's commented second webcam: another device node and no filter.
USB_2 = (
    "ffmpeg -hide_banner -loglevel warning "
    "-f v4l2 -input_format mjpeg -video_size 1280x720 -framerate 30 "
    "-i /dev/camera-usb-2 "
    "-c:v libx264 -preset ultrafast -tune zerolatency -bf 0 "
    "-pix_fmt yuv420p -g 30 -b:v 2M -maxrate 2M -bufsize 1M "
    "-f rtsp -rtsp_transport tcp rtsps://127.0.0.1:8322/usb-2"
)
TEST_PATTERN = (
    "ffmpeg -hide_banner -loglevel warning -re "
    "-f lavfi -i testsrc=size=1280x720:rate=30 "
    "-c:v libx264 -preset ultrafast -tune zerolatency -bf 0 "
    "-pix_fmt yuv420p -g 30 -b:v 2M -maxrate 2M -bufsize 1M "
    "-f rtsp -rtsp_transport tcp rtsps://127.0.0.1:8322/test-pattern"
)
CSI_KIT = {
    "name": "csi", "source": "rpiCamera", "rpiCameraWidth": 1920, "rpiCameraHeight": 1080,
    "rpiCameraFPS": 15, "rpiCameraCodec": "auto", "rpiCameraBitrate": 1500000, "rpiCameraIDRPeriod": 15,
    "rpiCameraHFlip": True,
}


def ffmpeg_path(command, name="usb"):
    return {"name": name, "source": "publisher", "runOnInit": command, "runOnInitRestart": True}


# -- kit detection -------------------------------------------------------------------------------


def test_an_rpicamera_source_is_the_csi_kit():
    assert kit_of(CSI_KIT) is Kit.CSI


@pytest.mark.parametrize("command", [USB_KIT, USB_2, TEST_PATTERN])
def test_an_ffmpeg_run_on_init_is_the_ffmpeg_kit(command):
    assert kit_of(ffmpeg_path(command)) is Kit.FFMPEG


@pytest.mark.parametrize("config", [
    {"name": "cam", "source": "rtsp://elsewhere/cam"},
    {"name": "cam", "source": "publisher"},
    {"name": "cam", "source": "publisher", "runOnInit": "gst-launch-1.0 videotestsrc ! fakesink"},
    {"name": "cam", "source": "publisher", "runOnInit": "ffmpeg -i in.mp4 -c copy -f rtsp rtsp://x/cam"},
    {"name": "cam", "source": "publisher", "runOnInit": 7},
    "not a config",
])
def test_a_path_that_is_neither_has_no_quality(config):
    assert kit_of(config) is None


# -- CSI ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("level", "fields"), [
    (Level.HIGH, (1920, 1080, 30, 4_000_000, 30)),
    (Level.MEDIUM, (1920, 1080, 15, 1_500_000, 15)),
    (Level.LOW, (1280, 720, 15, 800_000, 15)),
    (Level.MINIMUM, (1280, 720, 5, 300_000, 5)),
])
def test_each_level_is_five_rpicamera_fields_on_csi(level, fields):
    body = patch_for(Kit.CSI, level, CSI_KIT)

    assert body == dict(zip(["rpiCameraWidth", "rpiCameraHeight", "rpiCameraFPS", "rpiCameraBitrate",
                             "rpiCameraIDRPeriod"], fields, strict=True))


def test_a_csi_config_reads_back_to_its_level():
    assert LADDERS[Kit.CSI].level_of(live_setting(Kit.CSI, CSI_KIT)) is Level.MEDIUM
    assert LADDERS[Kit.CSI].level_of(live_setting(Kit.CSI, {**CSI_KIT, **patch_for(Kit.CSI, Level.LOW, CSI_KIT)})) \
        is Level.LOW


def test_a_csi_config_off_the_ladder_reads_back_as_no_level():
    off = {**CSI_KIT, "rpiCameraFPS": 25}

    assert live_setting(Kit.CSI, off) == Setting(1920, 1080, 25, 1_500_000, 15)
    assert LADDERS[Kit.CSI].level_of(live_setting(Kit.CSI, off)) is None


def test_a_csi_config_with_nonsense_values_has_no_setting():
    assert live_setting(Kit.CSI, {**CSI_KIT, "rpiCameraWidth": "<script>"}) is None
    assert live_setting(Kit.CSI, {**CSI_KIT, "rpiCameraFPS": True}) is None


# -- ffmpeg (USB and test-pattern) ---------------------------------------------------------------------


@pytest.mark.parametrize(("level", "size", "fps", "rate", "bufsize"), [
    (Level.HIGH, "1280x720", "30", "2M", "1M"),
    (Level.MEDIUM, "1280x720", "15", "1M", "500k"),
    (Level.LOW, "640x360", "15", "500k", "250k"),
    (Level.MINIMUM, "640x360", "10", "250k", "125k"),
])
def test_each_level_rewrites_only_the_usb_kits_size_rate_and_bitrate_arguments(level, size, fps, rate, bufsize):
    body = patch_for(Kit.FFMPEG, level, ffmpeg_path(USB_KIT))

    assert body == {"runOnInit": (
        "ffmpeg -hide_banner -loglevel warning "
        f"-f v4l2 -input_format mjpeg -video_size {size} -framerate {fps} "
        "-i /dev/camera-usb "
        "-vf $USB_CAMERA_FILTER "
        "-c:v libx264 -preset ultrafast -tune zerolatency -bf 0 "
        f"-pix_fmt yuv420p -g {fps} -b:v {rate} -maxrate {rate} -bufsize {bufsize} "
        "-f rtsp -rtsp_transport tcp rtsps://127.0.0.1:8322/usb")}


def test_the_rewrite_keeps_the_filter_variable_and_device_node_byte_for_byte():
    rewritten = patch_for(Kit.FFMPEG, Level.MINIMUM, ffmpeg_path(USB_KIT))["runOnInit"]

    assert "-i /dev/camera-usb -vf $USB_CAMERA_FILTER -c:v" in rewritten


def test_the_rewrite_keeps_a_second_webcams_node_and_its_own_target():
    rewritten = patch_for(Kit.FFMPEG, Level.LOW, ffmpeg_path(USB_2, "usb-2"))["runOnInit"]

    assert rewritten == USB_2.replace("1280x720", "640x360").replace("-framerate 30", "-framerate 15") \
        .replace("-g 30", "-g 15").replace("-b:v 2M -maxrate 2M -bufsize 1M", "-b:v 500k -maxrate 500k -bufsize 250k")


def test_the_rewrite_keeps_every_other_token_and_its_spacing():
    """Whatever the Device's string holds besides the six arguments survives,
    including a flip filter, odd spacing and arguments the kit never had."""
    command = ("ffmpeg  -hide_banner -f v4l2 -input_format mjpeg -video_size 1280x720  -framerate 30 "
               "-i /dev/v4l/by-id/usb-046d_C922-video-index0 -vf hflip,vflip -c:v libx264 -g 30 "
               "-b:v 2M -maxrate 2M -bufsize 1M -x264-params keyint=30 -f rtsp rtsps://127.0.0.1:8322/usb")

    rewritten = patch_for(Kit.FFMPEG, Level.LOW, ffmpeg_path(command))["runOnInit"]

    assert rewritten == ("ffmpeg  -hide_banner -f v4l2 -input_format mjpeg -video_size 640x360  -framerate 15 "
                         "-i /dev/v4l/by-id/usb-046d_C922-video-index0 -vf hflip,vflip -c:v libx264 -g 15 "
                         "-b:v 500k -maxrate 500k -bufsize 250k -x264-params keyint=30 -f rtsp "
                         "rtsps://127.0.0.1:8322/usb")


def test_the_test_patterns_lavfi_size_and_rate_follow_the_level():
    rewritten = patch_for(Kit.FFMPEG, Level.LOW, ffmpeg_path(TEST_PATTERN, "test-pattern"))["runOnInit"]

    assert "-f lavfi -i testsrc=size=640x360:rate=15 " in rewritten
    assert "-g 15 -b:v 500k -maxrate 500k -bufsize 250k" in rewritten
    assert rewritten.endswith(" rtsps://127.0.0.1:8322/test-pattern")


@pytest.mark.parametrize(("command", "level"), [
    (USB_KIT, Level.MEDIUM),
    (USB_2, Level.HIGH),
    (TEST_PATTERN, Level.HIGH),  # the test-pattern kit boots at the USB ladder's top
])
def test_an_ffmpeg_config_reads_back_to_its_level(command, level):
    assert LADDERS[Kit.FFMPEG].level_of(live_setting(Kit.FFMPEG, ffmpeg_path(command))) is level


@pytest.mark.parametrize("level", list(Level))
def test_every_level_written_reads_back_as_itself(level):
    for kit, config in ((Kit.CSI, CSI_KIT), (Kit.FFMPEG, ffmpeg_path(USB_KIT)), (Kit.FFMPEG, ffmpeg_path(TEST_PATTERN))):
        written = {**config, **patch_for(kit, level, config)}
        assert LADDERS[kit].level_of(live_setting(kit, written)) is level


def test_bitrates_read_back_in_any_form_ffmpeg_takes():
    command = re.sub(r"-b:v \S+", "-b:v 1000000", USB_KIT)
    assert live_setting(Kit.FFMPEG, ffmpeg_path(command)).bitrate == 1_000_000
    command = re.sub(r"-b:v \S+", "-b:v 1.5M", USB_KIT)
    assert live_setting(Kit.FFMPEG, ffmpeg_path(command)).bitrate == 1_500_000


# -- the ladder ------------------------------------------------------------------------------------------


def test_the_ladder_runs_high_to_minimum_and_never_past_either_end():
    ladder = LADDERS[Kit.CSI]

    assert [rung.level for rung in ladder.rungs] == [Level.HIGH, Level.MEDIUM, Level.LOW, Level.MINIMUM]
    assert ladder.below(Level.HIGH) is Level.MEDIUM
    assert ladder.below(Level.MINIMUM) is None
    assert ladder.above(Level.MEDIUM) is Level.HIGH
    assert ladder.above(Level.HIGH) is None


def test_on_csi_only_the_step_across_resolutions_drops_viewers():
    ladder = LADDERS[Kit.CSI]

    assert [rung.disruptive_below for rung in ladder.rungs[:-1]] == [False, True, False]
    assert not ladder.disrupts(Level.HIGH, Level.MEDIUM)
    assert not ladder.disrupts(Level.MINIMUM, Level.LOW)
    assert ladder.disrupts(Level.MEDIUM, Level.LOW)
    assert ladder.disrupts(Level.HIGH, Level.MINIMUM)


def test_on_usb_every_step_drops_viewers():
    ladder = LADDERS[Kit.FFMPEG]

    assert [rung.disruptive_below for rung in ladder.rungs[:-1]] == [True, True, True]
    assert ladder.disrupts(Level.HIGH, Level.MEDIUM)


def test_from_an_unknown_level_a_change_drops_viewers_unless_csi_keeps_its_resolution():
    assert LADDERS[Kit.FFMPEG].disrupts(None, Level.HIGH)
    assert not LADDERS[Kit.CSI].disrupts(None, Level.HIGH, current_setting=Setting(1920, 1080, 25, 2_000_000, 25))
    assert LADDERS[Kit.CSI].disrupts(None, Level.HIGH, current_setting=Setting(1280, 720, 25, 2_000_000, 25))
    assert LADDERS[Kit.CSI].disrupts(None, Level.HIGH)
