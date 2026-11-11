"""The instance's own UI certificate (ADR 0014): which names it covers, and
when it is kept or made again. Uses the host's openssl, as the image does, so
the fast tier needs no Docker."""

import subprocess

import pytest

from camera_viewer import ui_certificate
from camera_viewer.config import ConfigError, Settings
from harness.tls import make_certificate


def test_it_names_loopback_the_host_the_host_ip_and_the_extras_once_each():
    names = ui_certificate.subject_names("192.0.2.10", ("cv.lab.example", "localhost"), "lab-laptop")

    assert names == ("localhost", "127.0.0.1", "::1", "lab-laptop", "192.0.2.10", "cv.lab.example")


def test_a_container_id_hostname_is_left_out_because_it_changes_on_every_recreate():
    assert "3f2a9c0d1b7e" not in ui_certificate.subject_names("", (), "3f2a9c0d1b7e")


def test_it_is_made_once_then_kept(tmp_path):
    names = ("localhost", "127.0.0.1")
    cert, key = ui_certificate.ensure(tmp_path, names)
    first = cert.read_bytes()

    assert ui_certificate.ensure(tmp_path, names) == (cert, key)
    assert cert.read_bytes() == first
    assert key.stat().st_mode & 0o777 == 0o600


def test_a_change_of_names_makes_a_new_one(tmp_path):
    cert, _ = ui_certificate.ensure(tmp_path, ("localhost",))
    first = ui_certificate.fingerprint(cert)

    ui_certificate.ensure(tmp_path, ("localhost", "cv.lab.example"))

    assert ui_certificate.fingerprint(cert) != first


def test_one_about_to_expire_is_made_again(tmp_path):
    # make_certificate writes a one-day certificate, well inside the 30-day renewal window.
    made, made_key = make_certificate(tmp_path)
    made.replace(tmp_path / ui_certificate.GENERATED_CERT)
    made_key.replace(tmp_path / ui_certificate.GENERATED_KEY)
    (tmp_path / ui_certificate.GENERATED_NAMES).write_text("localhost\n")
    old = ui_certificate.fingerprint(tmp_path / ui_certificate.GENERATED_CERT)

    cert, _ = ui_certificate.ensure(tmp_path, ("localhost",))

    assert ui_certificate.fingerprint(cert) != old
    end = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-checkend", str(800 * 24 * 3600)],
                         capture_output=True)
    assert end.returncode == 0  # valid for the next 800 days at least


def test_the_fingerprint_is_sha256_in_the_form_browsers_show(tmp_path):
    cert, _ = ui_certificate.ensure(tmp_path, ("localhost",))
    expected = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-fingerprint", "-sha256"],
                              capture_output=True, text=True, check=True).stdout.split("=", 1)[1].strip()

    assert ui_certificate.fingerprint(cert) == expected


def test_tls_is_on_by_default_and_reads_its_settings():
    settings = Settings.from_env({"CV_TLS_SANS": "cv.lab.example, 192.0.2.7 other", "CV_HOST_IP": "192.0.2.10"})

    assert settings.tls and not settings.tls_supplied
    assert settings.tls_names == ("cv.lab.example", "192.0.2.7", "other")
    assert settings.host_ip == "192.0.2.10"
    assert not Settings.from_env({"CV_TLS": "OFF"}).tls


@pytest.mark.parametrize("env", [{"CV_TLS": "yes"}, {"CV_TLS_SANS": "under_score"}, {"CV_HOST_IP": "a b"}],
                         ids=["switch", "san", "host-ip"])
def test_a_tls_setting_it_cannot_use_names_itself(env):
    with pytest.raises(ConfigError, match=next(iter(env))):
        Settings.from_env(env)
