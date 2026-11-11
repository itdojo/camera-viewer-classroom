"""The Device Kit's certificate generator, run as a student would run it."""

import json
import os
import shutil
import stat
import subprocess

import pytest

from harness.devices import GEN_CERT, KIT_DIR


def copy_variant(tmp_path, name):
    """A copy of one kit variant, as a student's checkout holds it: beside the
    shared gps folder its compose.yaml includes."""
    variant = tmp_path / name
    shutil.copytree(KIT_DIR / name, variant, ignore=shutil.ignore_patterns("certs", ".env", "compose.override.yaml"))
    if not (tmp_path / "gps").exists():
        shutil.copytree(KIT_DIR / "gps", tmp_path / "gps")
    return variant


def gen_cert(variant_dir, *extra):
    return subprocess.run(
        [str(GEN_CERT), *extra, str(variant_dir)],
        capture_output=True, text=True, check=True, env={**os.environ, "NO_COLOR": "1"},
    )


def fingerprint_line(output):
    return next(line for line in output.splitlines() if "Fingerprint:" in line).split()[-1]


def test_prints_a_sha256_fingerprint_and_writes_secrets_readable_only_by_the_owner(tmp_path):
    result = gen_cert(tmp_path)

    fingerprint = fingerprint_line(result.stdout)
    assert len(fingerprint) == 64 and set(fingerprint) <= set("0123456789abcdef")
    assert stat.S_IMODE((tmp_path / "certs" / "server.key").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    assert "INSTANCE_PASS=" in (tmp_path / ".env").read_text()


def test_rerunning_keeps_the_certificate_and_credentials(tmp_path):
    first = gen_cert(tmp_path)
    env_before = (tmp_path / ".env").read_text()

    second = gen_cert(tmp_path)

    assert fingerprint_line(second.stdout) == fingerprint_line(first.stdout)
    assert (tmp_path / ".env").read_text() == env_before


def test_force_replaces_the_certificate_but_keeps_the_credentials(tmp_path):
    first = gen_cert(tmp_path)
    env_before = (tmp_path / ".env").read_text()

    forced = gen_cert(tmp_path, "--force")

    assert fingerprint_line(forced.stdout) != fingerprint_line(first.stdout)
    assert (tmp_path / ".env").read_text() == env_before


# -- the camera, for variants that capture from one (the USB kit) -----------

C922 = "usb-046d_C922_Pro_Stream_Webcam_2E4A0F8F-video-index0"


def usb_variant(tmp_path):
    return copy_variant(tmp_path, "usb")


def by_id_dir(tmp_path, *names):
    """A stand-in for /dev/v4l/by-id: each name links to a real character device."""
    directory = tmp_path / "by-id"
    directory.mkdir(exist_ok=True)
    for name in names:
        (directory / name).symlink_to("/dev/null")
    return directory


def gen_cert_finding_cameras_in(directory, variant, *extra):
    return subprocess.run(
        [str(GEN_CERT), *extra, str(variant)],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1", "V4L_BY_ID_DIR": str(directory)},
    )


def env_values(variant):
    lines = (variant / ".env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


def test_names_the_one_attached_webcam_by_its_stable_path(tmp_path):
    variant = usb_variant(tmp_path)
    cameras = by_id_dir(tmp_path, C922, C922.replace("index0", "index1"))

    result = gen_cert_finding_cameras_in(cameras, variant)

    assert result.returncode == 0, result.stdout + result.stderr
    assert env_values(variant)["USB_CAMERA"] == str(cameras / C922)
    assert str(cameras / C922) in result.stdout


def test_leaves_the_camera_unset_and_says_so_when_none_is_attached(tmp_path):
    variant = usb_variant(tmp_path)

    result = gen_cert_finding_cameras_in(by_id_dir(tmp_path), variant)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "USB_CAMERA" not in env_values(variant)
    assert "WARN" in result.stdout and "--camera" in result.stdout


def test_does_not_guess_between_two_webcams(tmp_path):
    variant = usb_variant(tmp_path)
    other = "usb-046d_HD_Pro_Webcam_C920_ABCDEF-video-index0"

    result = gen_cert_finding_cameras_in(by_id_dir(tmp_path, C922, other), variant)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "USB_CAMERA" not in env_values(variant)
    assert C922 in result.stdout and other in result.stdout and "--camera" in result.stdout


def test_camera_option_picks_the_camera_and_replaces_a_previous_choice(tmp_path):
    variant = usb_variant(tmp_path)
    cameras = by_id_dir(tmp_path, C922)
    gen_cert_finding_cameras_in(cameras, variant)
    credentials = {k: v for k, v in env_values(variant).items() if k != "USB_CAMERA"}

    result = gen_cert_finding_cameras_in(cameras, variant, "--camera", "/dev/null")

    assert result.returncode == 0, result.stdout + result.stderr
    assert env_values(variant) == {**credentials, "USB_CAMERA": "/dev/null"}
    assert stat.S_IMODE((variant / ".env").stat().st_mode) == 0o600


def test_rerunning_keeps_the_chosen_camera(tmp_path):
    variant = usb_variant(tmp_path)
    cameras = by_id_dir(tmp_path, C922)
    gen_cert_finding_cameras_in(cameras, variant, "--camera", "/dev/null")
    before = (variant / ".env").read_text()

    result = gen_cert_finding_cameras_in(cameras, variant)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (variant / ".env").read_text() == before


def test_camera_option_refuses_a_path_that_is_not_a_device(tmp_path):
    variant = usb_variant(tmp_path)

    result = gen_cert_finding_cameras_in(by_id_dir(tmp_path), variant, "--camera", str(tmp_path / "nope"))

    assert result.returncode != 0
    assert "--camera" in result.stderr


def test_camera_option_takes_a_bare_name_as_listed_by_ls(tmp_path):
    # `ls /dev/v4l/by-id` prints bare names; that is what students paste.
    variant = usb_variant(tmp_path)
    cameras = by_id_dir(tmp_path, C922, C922.replace("index0", "index1"))

    result = gen_cert_finding_cameras_in(cameras, variant, "--camera", C922)

    assert result.returncode == 0, result.stdout + result.stderr
    assert env_values(variant)["USB_CAMERA"] == str(cameras / C922)


def test_a_bad_camera_name_is_refused_with_full_paths_to_copy(tmp_path):
    variant = usb_variant(tmp_path)
    cameras = by_id_dir(tmp_path, C922, C922.replace("index0", "index1"))

    result = gen_cert_finding_cameras_in(cameras, variant, "--camera", "usb-no-such-webcam-video-index0")

    assert result.returncode != 0
    assert str(cameras / C922) in result.stderr
    assert C922.replace("index0", "index1") not in result.stderr
    assert not (variant / ".env").exists()


# -- the camera device nodes, for the unprivileged CSI kit --------------------

PI_5_NODES = ["media0", "media3", "video0", "video20", "v4l-subdev0"]


def dev_dir(tmp_path, *names, dma_heap=True):
    """A stand-in for /dev: each name links to a real character device, plus
    nodes the CSI kit must not be given."""
    directory = tmp_path / "dev"
    directory.mkdir(exist_ok=True)
    for name in (*names, "tty0", "gpiomem", "vchiq"):
        (directory / name).symlink_to("/dev/null")
    if dma_heap:
        (directory / "dma_heap").mkdir()
        (directory / "dma_heap" / "linux,cma").symlink_to("/dev/null")
    return directory


def csi_variant(tmp_path):
    return copy_variant(tmp_path, "csi")


def gen_cert_with_devices_in(directory, variant):
    return subprocess.run(
        [str(GEN_CERT), str(variant)],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1", "DEV_DIR": str(directory)},
    )


def rendered_devices(variant):
    """The device paths Docker would map, as `docker compose up` in the variant reads the kit."""
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=variant, capture_output=True, text=True, check=True,
    )
    mediamtx = json.loads(result.stdout)["services"]["mediamtx"]
    assert not mediamtx.get("privileged")
    return [device["source"] for device in mediamtx.get("devices", [])]


def test_lists_the_pis_camera_nodes_for_docker_compose_and_nothing_else(tmp_path):
    variant = csi_variant(tmp_path)
    devices = dev_dir(tmp_path, *PI_5_NODES)

    result = gen_cert_with_devices_in(devices, variant)

    assert result.returncode == 0, result.stdout + result.stderr
    assert rendered_devices(variant) == [
        str(devices / name) for name in ("media0", "media3", "video0", "video20", "v4l-subdev0", "dma_heap")]
    assert "6, in" in result.stdout


def test_rerunning_keeps_the_device_list_until_the_nodes_change(tmp_path):
    variant = csi_variant(tmp_path)
    devices = dev_dir(tmp_path, *PI_5_NODES)
    gen_cert_with_devices_in(devices, variant)
    override = variant / "compose.override.yaml"
    before = override.read_text()

    again = gen_cert_with_devices_in(devices, variant)

    assert again.returncode == 0, again.stdout + again.stderr
    assert override.read_text() == before
    assert "NOTHING TO DO" in again.stdout

    (devices / "video20").unlink()
    changed = gen_cert_with_devices_in(devices, variant)

    assert changed.returncode == 0, changed.stdout + changed.stderr
    assert str(devices / "video20") not in rendered_devices(variant)
    assert "NOTHING TO DO" not in changed.stdout


def test_with_no_camera_nodes_writes_no_override_and_says_so(tmp_path):
    variant = csi_variant(tmp_path)

    result = gen_cert_with_devices_in(dev_dir(tmp_path, dma_heap=False), variant)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (variant / "compose.override.yaml").exists()
    assert "WARN" in result.stdout and "on the Pi" in result.stdout


def test_leaves_an_override_it_did_not_write_alone_and_prints_the_list(tmp_path):
    variant = csi_variant(tmp_path)
    devices = dev_dir(tmp_path, *PI_5_NODES)
    override = variant / "compose.override.yaml"
    override.write_text("services:\n  mediamtx:\n    restart: always\n")

    result = gen_cert_with_devices_in(devices, variant)

    assert result.returncode == 0, result.stdout + result.stderr
    assert override.read_text() == "services:\n  mediamtx:\n    restart: always\n"
    assert "WARN" in result.stdout and str(devices / "media0") in result.stdout


@pytest.mark.parametrize("name", ["test-pattern", "usb"])
def test_only_the_csi_variant_gets_a_device_list(tmp_path, name):
    variant = copy_variant(tmp_path, name)

    result = gen_cert_with_devices_in(dev_dir(tmp_path, *PI_5_NODES), variant)

    assert not (variant / "compose.override.yaml").exists(), result.stdout + result.stderr


@pytest.mark.parametrize("name", ["test-pattern", "csi"])
def test_a_variant_without_a_webcam_gets_none(tmp_path, name):
    variant = copy_variant(tmp_path, name)

    result = gen_cert_finding_cameras_in(by_id_dir(tmp_path, C922), variant)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "USB_CAMERA" not in env_values(variant)


# -- the flip settings, written into a new .env commented out -----------------

def rendered_environment(variant):
    """The mediamtx service's environment, as `docker compose up` in the variant reads it."""
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=variant, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)["services"]["mediamtx"]["environment"]


def uncomment(variant, setting):
    env_file = variant / ".env"
    env_file.write_text(env_file.read_text().replace(f"#{setting}\n", f"{setting}\n"))


def test_the_camera_modules_flip_switches_wait_commented_out_in_a_new_env_file(tmp_path):
    variant = csi_variant(tmp_path)
    result = gen_cert_with_devices_in(dev_dir(tmp_path, *PI_5_NODES), variant)
    assert result.returncode == 0, result.stdout + result.stderr
    env_text = (variant / ".env").read_text()
    assert "#CAMERA_HFLIP=true\n" in env_text and "#CAMERA_VFLIP=true\n" in env_text
    assert rendered_environment(variant)["MTX_PATHS_CSI_RPICAMERAHFLIP"] == "false"

    uncomment(variant, "CAMERA_HFLIP=true")

    environment = rendered_environment(variant)
    assert environment["MTX_PATHS_CSI_RPICAMERAHFLIP"] == "true"
    assert environment["MTX_PATHS_CSI_RPICAMERAVFLIP"] == "false"


def test_the_webcams_flip_filter_waits_commented_out_in_a_new_env_file(tmp_path):
    variant = usb_variant(tmp_path)
    result = gen_cert_finding_cameras_in(by_id_dir(tmp_path, C922), variant)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "#USB_CAMERA_FILTER=hflip,vflip\n" in (variant / ".env").read_text()
    assert rendered_environment(variant)["USB_CAMERA_FILTER"] == "null"

    uncomment(variant, "USB_CAMERA_FILTER=hflip,vflip")

    assert rendered_environment(variant)["USB_CAMERA_FILTER"] == "hflip,vflip"


def test_the_test_pattern_gets_no_flip_settings(tmp_path):
    variant = copy_variant(tmp_path, "test-pattern")

    gen_cert(variant)

    assert "FLIP" not in (variant / ".env").read_text()


# -- the GNSS receiver, for the GPS Sidecar every variant can run -------------

U_BLOX = "usb-u-blox_AG_-_www.u-blox.com_u-blox_7_-_GPS_GNSS_Receiver-if00"
FTDI = "usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0"  # a plain USB-serial cable, not a receiver
QUECTEL = [f"usb-Quectel_EG25-G-if0{n}-port0" for n in range(4)]  # an LTE HAT: if01 is its NMEA port


def serial_dir(tmp_path, *names):
    """A stand-in for /dev/serial/by-id: each name links to a real character device."""
    directory = tmp_path / "serial-by-id"
    directory.mkdir(exist_ok=True)
    for name in names:
        (directory / name).symlink_to("/dev/null")
    return directory


def gen_cert_finding_receivers_in(directory, variant, *extra):
    return subprocess.run(
        [str(GEN_CERT), *extra, str(variant)],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1", "SERIAL_BY_ID_DIR": str(directory)},
    )


@pytest.mark.parametrize("name", ["test-pattern", "usb", "csi"])
def test_names_the_one_attached_gnss_receiver_by_its_stable_path_for_any_variant(tmp_path, name):
    variant = copy_variant(tmp_path, name)
    receivers = serial_dir(tmp_path, U_BLOX, FTDI)

    result = gen_cert_finding_receivers_in(receivers, variant)

    assert result.returncode == 0, result.stdout + result.stderr
    assert env_values(variant)["GPS_TTY"] == str(receivers / U_BLOX)
    assert str(receivers / U_BLOX) in result.stdout


def test_takes_an_lte_hats_nmea_port_and_not_its_modem_ports(tmp_path):
    variant = copy_variant(tmp_path, "test-pattern")
    receivers = serial_dir(tmp_path, *QUECTEL)

    result = gen_cert_finding_receivers_in(receivers, variant)

    assert result.returncode == 0, result.stdout + result.stderr
    assert env_values(variant)["GPS_TTY"] == str(receivers / QUECTEL[1])


@pytest.mark.parametrize("names", [[], [FTDI]], ids=["no-serial-devices", "serial-but-no-receiver"])
def test_with_no_gnss_receiver_leaves_gps_tty_unset_without_a_warning(tmp_path, names):
    variant = copy_variant(tmp_path, "test-pattern")

    result = gen_cert_finding_receivers_in(serial_dir(tmp_path, *names), variant)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "GPS_TTY" not in env_values(variant)
    assert "WARN" not in result.stdout


def test_with_no_serial_folder_at_all_leaves_gps_tty_unset_without_a_warning(tmp_path):
    variant = copy_variant(tmp_path, "test-pattern")

    result = gen_cert_finding_receivers_in(tmp_path / "no-such-dir", variant)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "GPS_TTY" not in env_values(variant)
    assert "WARN" not in result.stdout


def test_does_not_guess_between_two_gnss_receivers(tmp_path):
    variant = copy_variant(tmp_path, "test-pattern")
    other = "usb-u-blox_AG_u-blox_GNSS_receiver-if00"

    result = gen_cert_finding_receivers_in(serial_dir(tmp_path, U_BLOX, other), variant)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "GPS_TTY" not in env_values(variant)
    assert U_BLOX in result.stdout and other in result.stdout and "GPS_TTY" in result.stdout


def test_rerunning_keeps_a_receiver_already_in_the_env_file(tmp_path):
    variant = copy_variant(tmp_path, "test-pattern")
    gen_cert_finding_receivers_in(serial_dir(tmp_path), variant)
    with (variant / ".env").open("a") as env_file:
        env_file.write("GPS_TTY=/dev/ttyAMA0\n")
    before = (variant / ".env").read_text()

    result = gen_cert_finding_receivers_in(serial_dir(tmp_path, U_BLOX), variant)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (variant / ".env").read_text() == before
    assert "NOTHING TO DO" in result.stdout
