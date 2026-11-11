"""Bring Camera Viewer instances up and down for a test session.

An instance under test is the real thing: the image built from this repo's
Dockerfile, started the README's macOS way (``compose.yaml`` plus
``compose.macos.yaml``, ``CV_HOST_IP=127.0.0.1``) with
``instance-override.yaml`` on top. The overlay joins the session's Docker
network (shared with the session's Devices) and binds ephemeral loopback
ports:

- the UI on ``base_url`` (``https://127.0.0.1:<ephemeral>``, served with the
  certificate the instance made for itself, as it ships; ``http://`` for an
  instance started with ``tls=False``, which sets ``CV_TLS=off``);
- WebRTC media on a UDP port picked free on the host (``CV_WEBRTC_UDP_PORT``),
  published 1:1 and advertised by the Relay as ``CV_HOST_IP``, so Chromium on
  the host can play a Feed;
- ``config/`` and ``captures/`` bind-mounted from the session's temp dir,
  readable by tests as ``instance.config_dir`` and ``instance.captures_dir``.

One image per pytest session, tagged ``camera-viewer-test:<session>`` and
removed at the end. One compose project per instance,
``<session>-<name>``. Everything carries the session label.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import secrets
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from harness.devices import PROBE_IMAGE, REPO_ROOT, SESSION_LABEL, Device, DeviceHarness, _run
from harness.tls import make_certificate, trusting

GENERATED_CERT = "generated-cert.pem"  # camera_viewer.ui_certificate.GENERATED_CERT

COMPOSE = REPO_ROOT / "compose.yaml"
MACOS = REPO_ROOT / "compose.macos.yaml"
OVERRIDE = Path(__file__).with_name("instance-override.yaml")
SERVICE = "camera-viewer"
HTTP_PORT = 8080
STREAM_CONNECT_SECONDS = 10

# Run inside the instance by ``Instance.relay_get``: find the Relay's internal
# user in the backend's environment, GET argv[1] from the Relay's API, and
# print the status, a newline, then the body.
_RELAY_GET = """
import base64, glob, sys, urllib.error, urllib.request
for name in glob.glob("/proc/[0-9]*/environ"):
    try:
        env = dict(v.split("=", 1) for v in open(name, "rb").read().decode().split("\\0") if "=" in v)
    except OSError:
        continue
    if env.get("CV_RELAY_PASS"):
        break
else:
    sys.exit("no process holds CV_RELAY_PASS")
token = base64.b64encode(f"{env['CV_RELAY_USER']}:{env['CV_RELAY_PASS']}".encode()).decode()
base = env.get("CV_RELAY_API_URL", "http://127.0.0.1:19997")
request = urllib.request.Request(base + sys.argv[1], headers={"Authorization": "Basic " + token})
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        print(response.status, response.read().decode(), sep="\\n")
except urllib.error.HTTPError as error:
    print(error.code, error.read().decode(), sep="\\n")
"""


class InstanceStartError(RuntimeError):
    """An instance did not come up in time."""


def build_image(session_id: str) -> str:
    """Build the instance image from the working tree, tagged for this session."""
    tag = f"camera-viewer-test:{session_id}"
    _run(["docker", "build", "-q", "-t", tag, "--label", f"{SESSION_LABEL}={session_id}",
          str(REPO_ROOT)], timeout=900)
    return tag


def remove_image(tag: str) -> None:
    _run(["docker", "image", "rm", "-f", tag], check=False)


def run_image_once(image: str, session_id: str, *, env: Mapping[str, str] | None = None,
                   timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """Run the instance image bare (no compose, no ports, no mounts) and wait
    for it to exit. For checks that the image itself refuses a bad setup. A
    container still running at the timeout is removed, then TimeoutError."""
    name = f"cvt-{session_id}-once-{secrets.token_hex(3)}"
    cmd = ["docker", "run", "--rm", "--name", name, "--label", f"{SESSION_LABEL}={session_id}"]
    for key, value in (env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    try:
        return subprocess.run([*cmd, image], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _run(["docker", "rm", "-f", name], check=False)
        raise TimeoutError(f"{image} still running after {timeout}s") from exc


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class Instance:
    """One running Camera Viewer instance, as seen from a test."""

    name: str
    image: str
    workdir: Path
    harness: DeviceHarness
    env: Mapping[str, str] = field(default_factory=dict)
    tls: bool = True
    # Supply a harness certificate through CV_TLS_CERT/CV_TLS_KEY instead of
    # letting the instance make its own.
    certificate: bool = False
    host_ip: str = "127.0.0.1"
    webrtc_udp_port: int = field(default_factory=free_udp_port)
    _http_port: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # Every instance needs a shared password; tests that don't care get a random one.
        self.env = {"CV_PASSWORD": secrets.token_hex(8), **self.env}
        if not self.tls:
            self.env = {"CV_TLS": "off", **self.env}
        elif self.certificate:
            # A supplied certificate lives in the config folder, as the README suggests.
            self.env = {"CV_TLS_CERT": "/config/tls/cert.pem", "CV_TLS_KEY": "/config/tls/key.pem",
                        **self.env}

    @property
    def password(self) -> str:
        return self.env["CV_PASSWORD"]

    @property
    def project(self) -> str:
        return f"{self.harness.session_id}-{self.name}"

    @property
    def config_dir(self) -> Path:
        return self.workdir / "config"

    @property
    def captures_dir(self) -> Path:
        return self.workdir / "captures"

    @property
    def tls_cert(self) -> Path | None:
        """The UI certificate on the host, when the instance serves HTTPS."""
        if not self.tls:
            return None
        return self.config_dir / "tls" / ("cert.pem" if self.certificate else GENERATED_CERT)

    # -- addresses ----------------------------------------------------------

    @property
    def base_url(self) -> str:
        if self._http_port is None:
            out = self._compose("port", SERVICE, str(HTTP_PORT)).stdout.strip()
            self._http_port = int(out.splitlines()[-1].rsplit(":", 1)[1])
        return f"{'https' if self.tls else 'http'}://127.0.0.1:{self._http_port}"

    def _verify(self):
        return trusting(self.tls_cert) if self.tls else True

    def client(self, *, login: bool = True, **kwargs) -> httpx.Client:
        """An httpx client against the instance's HTTP API, logged in with the
        instance's password (its own fresh session) unless ``login=False``."""
        cookies = kwargs.pop("cookies", None)
        client = httpx.Client(base_url=self.base_url, timeout=kwargs.pop("timeout", 15),
                              verify=kwargs.pop("verify", self._verify()), **kwargs)
        if login:
            client.cookies.update(self.session_cookies())
        if cookies:
            client.cookies.update(cookies)
        return client

    def session_cookies(self) -> dict[str, str]:
        """Log in with the instance's password; returns the new session's cookie."""
        with httpx.Client(base_url=self.base_url, timeout=15, verify=self._verify()) as api:
            response = api.post("/api/session", json={"password": self.password})
            if response.status_code != 200:
                raise AssertionError(f"logging in to {self.name!r} answered "
                                     f"{response.status_code}: {response.text}")
            return dict(api.cookies)

    def browser_cookies(self) -> list[dict[str, Any]]:
        """A fresh session as Playwright cookies, for ``BrowserContext.add_cookies``."""
        return [{"name": name, "value": value, "url": self.base_url, "httpOnly": True, "sameSite": "Strict",
                 "secure": self.tls} for name, value in self.session_cookies().items()]

    def events(self, *, expect: int | None = 200, **kwargs) -> EventStream:
        """Subscribe to the instance's server-sent events (use as a context
        manager), logged in unless ``login=False``. Entering raises unless the
        stream answers ``expect``; pass ``expect=None`` to check
        ``status_code`` yourself (auth tests)."""
        return EventStream(self.client(timeout=httpx.Timeout(None, connect=STREAM_CONNECT_SECONDS), **kwargs),
                           expect=expect)

    # -- domain shortcuts ---------------------------------------------------

    def add_device(self, device: Device, *, label: str | None = None,
                   fingerprint: str | None = None) -> dict[str, Any]:
        """Add a harness Device the way a student would after reading its
        fingerprint off ``gen-cert.sh``: address, label, credentials and the
        confirmed fingerprint in one request. Returns the created Device."""
        with self.client() as api:
            response = api.post("/api/devices", json={
                "address": device.address,
                "label": label or device.label,
                "username": device.username,
                "password": device.password,
                "fingerprint": fingerprint or device.fingerprint,
            })
        assert response.status_code == 201, response.text
        return response.json()

    # -- lifecycle ------------------------------------------------------------

    def start(self, *, wait: bool = True) -> Instance:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        if self.certificate and not self.tls_cert.exists():
            make_certificate(self.tls_cert.parent)
        self._env_file.write_text("".join(f"{k}={v}\n" for k, v in self.env.items()))
        self._compose("up", "-d", "--no-build", "--quiet-pull")
        self._http_port = None
        if wait:
            self.wait_ready()
        return self

    def restart(self, *, wait: bool = True) -> None:
        self._compose("restart", "-t", "5")
        self._http_port = None
        if wait:
            self.wait_ready()

    def stop(self) -> None:
        self._compose("stop", "-t", "5")
        self._http_port = None

    def remove(self) -> None:
        self._compose("down", "-v", "--remove-orphans", "-t", "5")

    def logs(self) -> str:
        return self._compose("logs", "--no-color", check=False).stdout

    def exec(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run a command inside the instance container."""
        return self._compose("exec", "-T", SERVICE, *args, check=False)

    def relay_get(self, api_path: str) -> dict[str, Any] | None:
        """GET the Relay's own API (``/v3/...``, path segments already quoted)
        from inside the container, for counters the instance does not expose,
        such as a static source's ``inboundRTPPacketsLost``. None on a 404.

        The Relay takes only its internal user, generated at every start
        (ADR 0006), so this reads it from the backend's environment, as only
        someone who can already ``docker exec`` into the instance could."""
        result = self.exec("python", "-c", _RELAY_GET, api_path)
        status, _, body = result.stdout.partition("\n")
        if result.returncode != 0 or not status.isdigit():
            raise RuntimeError(f"could not read the Relay API at {api_path}: {result.stdout}{result.stderr}")
        if status == "404":
            return None
        if status != "200":
            raise RuntimeError(f"Relay API {api_path} answered {status}: {body}")
        return json.loads(body)

    def cpu_seconds(self) -> dict[str, float]:
        """CPU time (user + system) used so far by each process in the
        instance container, summed by command name (``mediamtx`` is the Relay,
        ``python`` the backend and its supervisor). Diff two readings for a
        rate: one container holds both, so ``docker stats`` cannot split them."""
        out = self.exec("sh", "-c", "cat /proc/[0-9]*/stat 2>/dev/null").stdout
        ticks = 100  # USER_HZ on Linux, arm64 and amd64 alike
        usage: dict[str, float] = {}
        for line in out.splitlines():
            comm, _, rest = line.partition(" (")[2].rpartition(") ")
            fields = rest.split()
            if comm in ("sh", "cat") or len(fields) < 13:
                continue
            usage[comm] = usage.get(comm, 0) + (int(fields[11]) + int(fields[12])) / ticks
        return usage

    def docker_cpu_percent(self) -> float:
        """One ``docker stats`` sample of the whole container, in percent of
        one core."""
        name = self._compose("ps", "-q", SERVICE).stdout.strip()
        out = _run(["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", name]).stdout
        return float(out.strip().rstrip("%"))

    def probe_capture(self, name: str) -> dict[str, Any]:
        """ffprobe a file in the captures folder (from a throwaway container,
        so the host needs no ffmpeg): ``{"streams": [...], "format": {...}}``.
        Raises if ffprobe cannot read it, which is what "not playable" means."""
        result = _run(["docker", "run", "--rm", "--label", f"{SESSION_LABEL}={self.harness.session_id}",
                       "-v", f"{self.captures_dir}:/captures:ro", "--entrypoint", "ffprobe", PROBE_IMAGE,
                       "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                       f"/captures/{name}"], check=False, timeout=60)
        assert result.returncode == 0, f"ffprobe could not read {name}: {result.stderr}"
        return json.loads(result.stdout)

    def wait_ready(self, timeout: float = 60) -> None:
        """Block until the backend serves the UI."""
        deadline = time.monotonic() + timeout
        last_error = "never answered"
        while time.monotonic() < deadline:
            try:
                with self.client(timeout=3, login=False) as api:
                    response = api.get("/login")
                if response.status_code < 500:
                    return
                last_error = f"HTTP {response.status_code}"
            # OSError: the instance has not written its certificate yet.
            except (httpx.HTTPError, RuntimeError, IndexError, OSError) as exc:
                self._http_port = None
                last_error = repr(exc)
                if self._state() == "exited":
                    break
            time.sleep(0.5)
        raise InstanceStartError(f"instance {self.name!r} not ready after {timeout}s: {last_error}\n"
                                 f"{self.logs()}")

    # -- internals ------------------------------------------------------------

    def _state(self) -> str:
        out = self._compose("ps", "-a", "--format", "{{.State}}", SERVICE, check=False).stdout
        return out.strip()

    @property
    def _env_file(self) -> Path:
        return self.workdir / "instance.env"

    def _compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "CV_IMAGE": self.image,
            "CV_TEST_SESSION": self.harness.session_id,
            "CV_TEST_NETWORK": self.harness.network,
            "CV_TEST_CONFIG_DIR": str(self.config_dir),
            "CV_TEST_CAPTURES_DIR": str(self.captures_dir),
            "CV_TEST_ENV_FILE": str(self._env_file),
            "CV_TEST_UID": str(os.getuid()),
            "CV_TEST_GID": str(os.getgid()),
            "CV_WEBRTC_UDP_PORT": str(self.webrtc_udp_port),
            "CV_PASSWORD": self.password,  # compose.yaml requires it
            "CV_HOST_IP": self.host_ip,  # the macOS overlay's one variable
            # compose.yaml takes TZ from the invoking shell; pin it so Capture
            # names don't depend on the developer's environment.
            "TZ": self.env.get("TZ", "UTC"),
        }
        cmd = ["docker", "compose", "-p", self.project,
               "-f", str(COMPOSE), "-f", str(MACOS), "-f", str(OVERRIDE), *args]
        return _run(cmd, env=env, check=check, timeout=180)


class InstancePool:
    """Every instance a session starts, so they all come down at the end."""

    def __init__(self, harness: DeviceHarness, image: str, workdir: Path):
        self.harness = harness
        self.image = image
        self.workdir = Path(workdir)
        self.instances: dict[str, Instance] = {}
        self._on_start: list[Callable[[Instance], None]] = []
        atexit.register(self.close)

    def on_start(self, listener: Callable[[Instance], None]) -> None:
        """Call ``listener(instance)`` whenever an instance has started and is
        ready (the logged-in browsers use it to log in to new instances)."""
        self._on_start.append(listener)

    def start(self, name: str, *, env: Mapping[str, str] | None = None, tls: bool = True,
              certificate: bool = False, host_ip: str = "127.0.0.1", wait: bool = True) -> Instance:
        """Start an instance. It serves HTTPS with a certificate it makes for
        itself unless ``tls=False`` (``CV_TLS=off``); ``certificate=True``
        supplies a fresh self-signed one through the environment instead.
        ``host_ip`` is the macOS overlay's ``CV_HOST_IP``."""
        if name in self.instances:
            raise ValueError(f"instance {name!r} already exists in this session")
        instance = Instance(name=name, image=self.image, workdir=self.workdir / name,
                            harness=self.harness, env=dict(env or {}), tls=tls, certificate=certificate,
                            host_ip=host_ip)
        self.instances[name] = instance
        instance.start(wait=wait)
        if wait:
            for listener in self._on_start:
                listener(instance)
        return instance

    def remove(self, instance: Instance) -> None:
        instance.remove()
        self.instances.pop(instance.name, None)

    def close(self) -> None:
        errors = []
        for instance in list(self.instances.values()):
            try:
                self.remove(instance)
            except Exception as exc:  # keep tearing down the rest
                errors.append(exc)
        atexit.unregister(self.close)
        if errors:
            raise RuntimeError(f"instance teardown left debris: {errors}")


_BROKEN = object()  # queued by the reader when the stream fails or ends


class EventStream:
    """A server-sent-events subscription read on a background thread.

    ``next()`` returns the data (parsed JSON) of the next event matching a
    name and predicate, skipping everything else, including the snapshot
    events the stream opens with. It raises at once if the stream broke or
    ended, rather than waiting out its timeout.

    Leaving the context stops the reader before returning. Closing the
    response from this thread is not enough: on Linux a read blocked in
    another thread outlives ``close()``, the next connection reuses its
    descriptor, and over TLS the stale read corrupts that connection's
    records. So the socket is shut down, which ends the blocked read, and the
    reader, which owns the response, is joined.
    """

    def __init__(self, client: httpx.Client, path: str = "/api/events", expect: int | None = 200):
        self._client = client
        self._expect = expect
        self._path = path
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._connected = threading.Event()
        self._response: httpx.Response | None = None
        self._error: BaseException | None = None
        self._closing = False
        self._thread = threading.Thread(target=self._read, daemon=True)

    def __enter__(self) -> EventStream:
        self._thread.start()
        if not self._connected.wait(STREAM_CONNECT_SECONDS + 5):
            self.__exit__()
            raise TimeoutError(f"event stream never connected: {self._error!r}")
        if self._error:
            self.__exit__()
            raise self._error
        if self._expect is not None and self.status_code != self._expect:
            self.__exit__()
            raise AssertionError(f"event stream answered {self.status_code}, expected {self._expect}")
        return self

    def __exit__(self, *exc_info) -> None:
        self._closing = True
        if self._thread.is_alive() and self._response is not None:
            stream = self._response.extensions["network_stream"]
            try:
                stream.get_extra_info("socket").shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # the reader closed it meanwhile; a closed socket object no longer names the descriptor
        self._thread.join(STREAM_CONNECT_SECONDS + 5)
        self._client.close()
        if self._thread.is_alive():
            raise RuntimeError("the event stream's reader is still running after the stream was closed")

    @property
    def status_code(self) -> int | None:
        return self._response.status_code if self._response is not None else None

    def next(self, event: str | None = None, where: Callable[[Any], bool] = lambda data: True,
             timeout: float = 10) -> Any:
        deadline = time.monotonic() + timeout
        seen = []
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                name, data = self._events.get(timeout=remaining)
            except queue.Empty:
                break
            if name is _BROKEN:
                raise RuntimeError(f"event stream broke after {seen}: {data}") from data
            seen.append(name)
            if (event is None or name == event) and where(data):
                return data
        raise TimeoutError(f"no matching {event!r} event within {timeout}s; saw {seen}")

    def _read(self) -> None:
        try:
            with self._client.stream("GET", self._path) as response:
                self._response = response
                self._connected.set()
                if self._closing:
                    return  # given up on while connecting
                name, data = "message", []
                for line in response.iter_lines():
                    if line == "":
                        if data:
                            payload = "\n".join(data)
                            try:
                                parsed = json.loads(payload)
                            except ValueError:
                                parsed = payload
                            self._events.put((name, parsed))
                        name, data = "message", []
                    elif line.startswith(":"):
                        continue
                    elif line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].lstrip())
            if not self._closing and response.is_success:  # a refusal (401) was never a stream
                self._events.put((_BROKEN, EOFError("the instance ended the stream")))
        except (httpx.HTTPError, RuntimeError, AttributeError, ValueError, OSError) as exc:
            if not self._closing:
                self._error = exc
                self._events.put((_BROKEN, exc))
        finally:
            self._connected.set()
