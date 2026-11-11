"""The UI's certificate (ADR 0014): the one supplied through ``CV_TLS_CERT``
and ``CV_TLS_KEY``, or else one the instance makes for itself.

The instance's own is a self-signed EC P-256 leaf in ``<config>/tls/``, made
with the image's openssl. It is kept across restarts, so its fingerprint
stays what browsers already accepted, and made again only when the names it
should cover change or it is within 30 days of expiring. 825 days is the
longest validity Apple's platforms accept for a TLS server certificate, even
one trusted by hand.

It names ``localhost``, both loopback addresses, the host's name (unless it
is a container ID, which changes on every recreate), ``CV_HOST_IP`` and
``CV_TLS_SANS``. A browser still warns about it until someone accepts it, and
the supervisor logs its fingerprint so that person can check what they accept.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
import ssl
import subprocess
from pathlib import Path

GENERATED_CERT = "generated-cert.pem"
GENERATED_KEY = "generated-key.pem"
# The names the generated certificate was made for, one per line, so a change is noticed.
GENERATED_NAMES = "generated-names"
VALIDITY_DAYS = 825
RENEW_BEFORE_SECONDS = 30 * 24 * 3600
# What Docker names a container's host when nothing else does: the first 12 hex digits of its ID.
CONTAINER_HOSTNAME = re.compile(r"^[0-9a-f]{12}$")
DNS_NAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


class CertificateError(Exception):
    """The instance could not make or read its UI certificate."""


def valid_name(name: str) -> bool:
    """An IP address or a DNS name a certificate can carry."""
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return bool(DNS_NAME.match(name))


def subject_names(host_ip: str, extra: tuple[str, ...], hostname: str) -> tuple[str, ...]:
    """Every name the generated certificate covers, in a stable order, without repeats."""
    names = ["localhost", "127.0.0.1", "::1"]
    if hostname and not CONTAINER_HOSTNAME.match(hostname) and valid_name(hostname):
        names.append(hostname)
    if host_ip:
        names.append(host_ip)
    names += extra
    return tuple(dict.fromkeys(names))


def ensure(directory: Path, names: tuple[str, ...]) -> tuple[Path, Path]:
    """The generated certificate and key in ``directory``, made first unless the
    ones there already cover exactly ``names`` and are not about to expire."""
    cert, key, record = directory / GENERATED_CERT, directory / GENERATED_KEY, directory / GENERATED_NAMES
    wanted = "".join(f"{name}\n" for name in names)
    try:
        current = cert.is_file() and key.is_file() and record.read_text() == wanted and not _expiring(cert)
    except OSError:
        current = False
    if not current:
        _generate(directory, names)
        try:
            record.write_text(wanted)
            record.chmod(0o600)  # like everything else in the config folder
        except OSError as exc:
            raise CertificateError(f"could not write {record}: {exc}") from None
    return cert, key


def fingerprint(cert: Path) -> str:
    """SHA-256 of the certificate's DER, as browsers show it (``AB:CD:...``)."""
    try:
        der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    except (OSError, ValueError) as exc:
        raise CertificateError(f"could not read the UI certificate {cert}: {exc}") from None
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def hostname() -> str:
    return socket.gethostname()


def _subject_alt_names(names: tuple[str, ...]) -> str:
    def entry(name: str) -> str:
        try:
            ipaddress.ip_address(name)
            return f"IP:{name}"
        except ValueError:
            return f"DNS:{name}"
    return ",".join(entry(name) for name in names)


def _expiring(cert: Path) -> bool:
    result = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-checkend", str(RENEW_BEFORE_SECONDS)],
                            capture_output=True, text=True)
    return result.returncode != 0


def _generate(directory: Path, names: tuple[str, ...]) -> None:
    """Write a new pair through temporary files, so a failure never leaves half of one."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CertificateError(f"could not create {directory} for the UI certificate: {exc}") from None
    cert_tmp, key_tmp = directory / f".{GENERATED_CERT}.tmp", directory / f".{GENERATED_KEY}.tmp"
    command = ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
               "-keyout", str(key_tmp), "-out", str(cert_tmp), "-days", str(VALIDITY_DAYS),
               "-subj", "/CN=camera-viewer",
               "-addext", f"subjectAltName={_subject_alt_names(names)}",
               # A leaf, not a CA: Firefox refuses a CA certificate presented as a server's.
               "-addext", "basicConstraints=critical,CA:FALSE",
               "-addext", "keyUsage=critical,digitalSignature",
               "-addext", "extendedKeyUsage=serverAuth"]
    old_umask = os.umask(0o077)
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError as exc:
        raise CertificateError(f"could not run openssl to make the UI certificate: {exc}") from None
    finally:
        os.umask(old_umask)
    if result.returncode != 0:
        raise CertificateError(f"openssl could not make the UI certificate: {result.stderr.strip()}")
    try:
        key_tmp.chmod(0o600)
        key_tmp.replace(directory / GENERATED_KEY)
        cert_tmp.replace(directory / GENERATED_CERT)
    except OSError as exc:
        raise CertificateError(f"could not write the UI certificate in {directory}: {exc}") from None
