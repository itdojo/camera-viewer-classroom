"""A plain-RTSP server that only listens: it answers every request with a
401 Basic challenge and logs the headers it received, so a test can see
whether the Relay sent credentials to a host.

Runs in a container on the session network under a DNS alias (any image
with Python 3; the instance image has it). Use as a context manager::

    with RtspSniffer(device_harness, instance_image, "sniff-1a2b3c") as sniffer:
        ...  # make the Relay pull rtsp://sniff-1a2b3c:8554/<anything>
        sniffer.requests()  # [{"line": "DESCRIBE rtsp://... RTSP/1.0", "headers": {...}}, ...]
"""

from __future__ import annotations

import json
import time

from harness.devices import SESSION_LABEL, DeviceHarness, _run

PORT = 8554

SERVER = r'''
import json, socket, threading

def handle(conn):
    stream = conn.makefile("rb")
    while True:
        lines = []
        while True:
            line = stream.readline()
            if not line:
                return
            if line in (b"\r\n", b"\n"):
                break
            lines.append(line.decode(errors="replace").rstrip("\r\n"))
        if not lines:
            continue
        headers = dict(l.split(":", 1) for l in lines[1:] if ":" in l)
        headers = {k.strip().lower(): v.strip() for k, v in headers.items()}
        print("REQUEST " + json.dumps({"line": lines[0], "headers": headers}), flush=True)
        conn.sendall(("RTSP/1.0 401 Unauthorized\r\nCSeq: " + headers.get("cseq", "0") + "\r\n"
                      "WWW-Authenticate: Basic realm=\"sniffer\"\r\n\r\n").encode())

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("0.0.0.0", %d))
server.listen()
print("LISTENING", flush=True)
while True:
    conn, _ = server.accept()
    threading.Thread(target=handle, args=(conn,), daemon=True).start()
''' % PORT  # noqa: UP031 (the embedded server is full of braces a format string would need doubled)


class RtspSniffer:
    def __init__(self, harness: DeviceHarness, image: str, alias: str):
        self.alias = alias
        self.container = f"{harness.session_id}-{alias}"
        self._harness = harness
        self._image = image

    @property
    def address(self) -> str:
        return self.alias

    def url(self, path: str = "cam") -> str:
        return f"rtsp://{self.alias}:{PORT}/{path}"

    def __enter__(self) -> RtspSniffer:
        _run(["docker", "run", "-d", "--name", self.container, "--network", self._harness.network,
              "--network-alias", self.alias, "--label", f"{SESSION_LABEL}={self._harness.session_id}",
              "--entrypoint", "python", self._image, "-u", "-c", SERVER])
        deadline = time.monotonic() + 30
        while "LISTENING" not in self._logs():
            if time.monotonic() > deadline:
                raise RuntimeError(f"RTSP sniffer never listened:\n{self._logs()}")
            time.sleep(0.2)
        return self

    def __exit__(self, *exc_info) -> None:
        _run(["docker", "rm", "-f", self.container], check=False)

    def requests(self) -> list[dict]:
        return [json.loads(line.split(" ", 1)[1]) for line in self._logs().splitlines()
                if line.startswith("REQUEST ")]

    def _logs(self) -> str:
        result = _run(["docker", "logs", self.container], check=False)
        return result.stdout + result.stderr
