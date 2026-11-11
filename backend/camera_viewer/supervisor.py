"""The image's entrypoint: runs the Relay and the backend side by side (ADR 0002).

Deliberately small. It refuses to start at all without the settings the
instance needs (a shared password, settings it can read), then starts both processes, restarts whichever one exits
(after a short pause, so a crash loop doesn't spin), and on SIGTERM or SIGINT
stops both and exits. A Relay restart loses every path configured through its
API; the backend's Relay Controller notices and re-applies them.

At every start it also makes up the Relay's internal user, the only one the
Relay lets use its API, WHEP and RTSP servers, and hands it to the Relay
(``MTX_AUTHINTERNALUSERS_0_USER``/``_PASS``, replacing the unusable
placeholder in ``relay.yml``) and to the backend (``CV_RELAY_USER``/``_PASS``).
Nothing else learns it, so a local process on the host's loopback can't
drive the Relay.

Before either starts it settles the UI's certificate (ADR 0014): the one
supplied, or the instance's own, made or kept in ``<config>/tls/``. Both
children get its paths as ``CV_TLS_CERT``/``CV_TLS_KEY``, the backend to serve
it and the Relay to pass on to its segment hooks, which trust exactly it. The
UI's URL and the certificate's fingerprint are logged, so whoever accepts the
browser's warning can check it.

Run as ``python -m camera_viewer.supervisor``.
"""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from camera_viewer import ui_certificate
from camera_viewer.config import ConfigError, Settings, check

RESTART_PAUSE_SECONDS = 1.0
STOP_GRACE_SECONDS = 8.0


def log(message: str) -> None:
    print(f"[supervisor] {message}", flush=True)


@dataclass
class Child:
    name: str
    argv: list[str]
    env: dict[str, str] | None = field(default=None, repr=False)
    process: subprocess.Popen | None = field(default=None, repr=False)
    restart_at: float = 0.0

    def start(self) -> None:
        log(f"starting {self.name}")
        self.process = subprocess.Popen(self.argv, env=self.env)

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None


RELAY_USER = "camera-viewer"


def children(settings: Settings, certificate: tuple[Path, Path] | None = None,
             relay_pass: str | None = None) -> list[Child]:
    """The Relay and the backend, given the UI certificate's (cert, key) when TLS is on."""
    relay_pass = relay_pass or secrets.token_urlsafe(24)
    tls_env = {"CV_TLS_CERT": str(certificate[0]), "CV_TLS_KEY": str(certificate[1])} if certificate else {}
    relay_env = {**os.environ, **tls_env, "MTX_AUTHINTERNALUSERS_0_USER": RELAY_USER,
                 "MTX_AUTHINTERNALUSERS_0_PASS": relay_pass}
    backend_env = {**os.environ, **tls_env, "CV_RELAY_USER": RELAY_USER, "CV_RELAY_PASS": relay_pass}
    return [
        Child("relay", ["mediamtx", str(settings.relay_config)], relay_env),
        Child("backend", [sys.executable, "-m", "camera_viewer.listener"], backend_env),
    ]


def run(procs: list[Child]) -> int:
    stopping = False

    def stop(signum, _frame):
        nonlocal stopping
        log(f"received signal {signum}; stopping")
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    for child in procs:
        child.start()
    while not stopping:
        now = time.monotonic()
        for child in procs:
            if child.running():
                continue
            if child.restart_at == 0.0:
                log(f"{child.name} exited with {child.process.returncode}; restarting")
                child.restart_at = now + RESTART_PAUSE_SECONDS
            elif now >= child.restart_at:
                child.restart_at = 0.0
                child.start()
        time.sleep(0.2)

    for child in procs:
        if child.running():
            child.process.terminate()
    deadline = time.monotonic() + STOP_GRACE_SECONDS
    for child in procs:
        if child.process is None:
            continue
        try:
            child.process.wait(max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.process.kill()
            child.process.wait()
    log("stopped")
    return 0


def certificate_for(settings: Settings) -> tuple[Path, Path]:
    """The UI's (cert, key): the supplied pair, else the instance's own."""
    if settings.tls_supplied:
        return settings.tls_cert, settings.tls_key
    names = ui_certificate.subject_names(settings.host_ip, settings.tls_names, ui_certificate.hostname())
    return ui_certificate.ensure(settings.config_dir / "tls", names)


def ui_url(settings: Settings) -> str:
    host = settings.host_ip or ui_certificate.hostname()
    if ui_certificate.CONTAINER_HOSTNAME.match(host):
        host = "<this host>"
    elif ":" in host:
        host = f"[{host}]"
    return f"{'https' if settings.tls else 'http'}://{host}:{settings.http_port}"


def main() -> None:
    try:
        settings = Settings.from_env()
        check(settings)
        certificate = certificate_for(settings) if settings.tls else None
        fingerprint = ui_certificate.fingerprint(certificate[0]) if certificate else None
    except (ConfigError, ui_certificate.CertificateError) as exc:
        log(f"refusing to start: {exc}")
        sys.exit(2)
    if fingerprint:
        log(f"serving the UI at {ui_url(settings)}; its certificate's SHA-256 fingerprint is {fingerprint}")
    else:
        log(f"serving the UI over plain HTTP at {ui_url(settings)} (CV_TLS is off)")
    sys.exit(run(children(settings, certificate)))


if __name__ == "__main__":
    main()
