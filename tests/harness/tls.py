"""Throwaway UI certificates for instances under test. Generated per test
session with the host's openssl, never committed."""

from __future__ import annotations

import ssl
from pathlib import Path

from harness.devices import _run

CERT_NAME = "cert.pem"
KEY_NAME = "key.pem"


def make_certificate(directory: Path) -> tuple[Path, Path]:
    """Write a self-signed EC P-256 certificate for 127.0.0.1 and localhost
    (valid one day) and its key into ``directory``; returns (cert, key)."""
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory / CERT_NAME, directory / KEY_NAME
    _run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
          "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1",
          "-subj", "/CN=camera-viewer-test",
          "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"])
    key.chmod(0o600)
    return cert, key


def trusting(cert: Path) -> ssl.SSLContext:
    """An SSL context that trusts exactly ``cert``."""
    return ssl.create_default_context(cafile=str(cert))
