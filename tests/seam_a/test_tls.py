"""Seam A: the UI is HTTPS by default (ADR 0014). With no certificate supplied
the instance makes its own in config/tls/, keeps it across restarts and makes
a new one when the names it should cover change; a supplied certificate is
served instead; ``CV_TLS=off`` serves plain HTTP. Plain HTTP on an HTTPS port
is redirected (``test_plain_http_redirect.py``)."""

import hashlib
import ssl
import subprocess

import pytest

from harness.instance import run_image_once


def fingerprint(cert):
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def subject_alt_names(cert):
    out = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-ext", "subjectAltName"],
                         capture_output=True, text=True, check=True).stdout
    return {name.strip() for name in out.splitlines()[-1].split(",")}


def test_by_default_the_ui_is_https_with_a_certificate_the_instance_made(instance):
    assert instance.base_url.startswith("https://")
    assert instance.tls_cert.name == "generated-cert.pem"

    # The client trusts exactly the certificate the instance wrote into its
    # config folder, so a handshake proves that is the one it serves.
    with instance.client(login=False) as api:
        assert api.get("/login").status_code == 200
        login = api.post("/api/session", json={"password": instance.password})
    assert login.status_code == 200
    assert "secure" in login.headers["set-cookie"].lower()
    with instance.client() as api:
        assert api.get("/api/cameras").status_code == 200


def test_the_instance_logs_its_certificate_fingerprint(instance):
    assert fingerprint(instance.tls_cert) in instance.logs()


def test_the_generated_certificate_is_a_server_leaf_for_its_names_and_its_key_is_private(instances):
    started = instances.start("generated", env={"CV_TLS_SANS": "camera-viewer.lab.example, 192.0.2.7"})

    assert {"DNS:localhost", "IP Address:127.0.0.1", "IP Address:0:0:0:0:0:0:0:1",
            "DNS:camera-viewer.lab.example", "IP Address:192.0.2.7"} <= subject_alt_names(started.tls_cert)
    text = subprocess.run(["openssl", "x509", "-in", str(started.tls_cert), "-noout", "-text"],
                          capture_output=True, text=True, check=True).stdout
    assert "CA:FALSE" in text
    assert "TLS Web Server Authentication" in text
    assert (started.tls_cert.parent / "generated-key.pem").stat().st_mode & 0o077 == 0


def test_the_generated_certificate_survives_a_restart(instances):
    started = instances.start("kept")
    before = fingerprint(started.tls_cert)

    started.restart()

    assert fingerprint(started.tls_cert) == before
    with started.client(login=False) as api:
        assert api.get("/login").status_code == 200


def test_changing_the_names_makes_a_new_certificate(instances):
    started = instances.start("renamed")
    before = fingerprint(started.tls_cert)

    started.env = {**started.env, "CV_TLS_SANS": "renamed.lab.example"}
    started.start()

    assert fingerprint(started.tls_cert) != before
    assert "DNS:renamed.lab.example" in subject_alt_names(started.tls_cert)
    with started.client(login=False) as api:
        assert api.get("/login").status_code == 200


def test_a_supplied_certificate_is_served_instead(instances):
    supplied = instances.start("supplied", certificate=True)

    assert supplied.tls_cert.name == "cert.pem"
    with supplied.client(login=False) as api:
        assert api.get("/login").status_code == 200
    assert not (supplied.config_dir / "tls" / "generated-cert.pem").exists()
    assert fingerprint(supplied.tls_cert) in supplied.logs()


def test_with_tls_off_the_ui_is_plain_http(instances):
    plain = instances.start("plain", tls=False)

    assert plain.base_url.startswith("http://")
    with plain.client(login=False) as api:
        assert api.get("/login").status_code == 200
        login = api.post("/api/session", json={"password": plain.password})
    assert "secure" not in login.headers["set-cookie"].lower()
    assert not (plain.config_dir / "tls").exists()


@pytest.mark.parametrize("env", [
    {"CV_TLS_CERT": "/config/tls/cert.pem"},
    {"CV_TLS_KEY": "/config/tls/key.pem"},
    {"CV_TLS_CERT": "/config/tls/missing.pem", "CV_TLS_KEY": "/config/tls/missing.key"},
    {"CV_TLS": "off", "CV_TLS_CERT": "/config/tls/cert.pem", "CV_TLS_KEY": "/config/tls/key.pem"},
    {"CV_TLS": "maybe"},
    {"CV_TLS_SANS": "camera viewer!"},
], ids=["cert-without-key", "key-without-cert", "files-missing", "off-with-a-certificate", "not-on-or-off",
        "a-name-no-certificate-can-carry"])
def test_the_instance_refuses_to_start_with_a_tls_setting_it_cannot_use(instance_image, device_harness, env):
    result = run_image_once(instance_image, device_harness.session_id,
                            env={"CV_PASSWORD": "pw", **env})
    output = result.stdout + result.stderr

    assert result.returncode == 2, output
    assert "refusing to start" in output
    assert "CV_TLS" in output
    assert "Traceback" not in output
