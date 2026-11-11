"""A fake Device API that answers whatever a test scripted: a Device is untrusted input.

Runs inside a container on the session network (see
``DeviceHarness.add_scripted_device``), serving HTTPS on 9997 with a
certificate from the kit's own ``gen-cert.sh``, so the instance pins and
talks to it exactly as it would a real Device. It answers from the JSON file
named on the command line:

    {"paths": [<items for /v3/paths/list>], "global": {<config/global/get>},
     "path_configs": {<name>: <config/paths/get body>}, "started": "<RFC 3339>"}

Besides discovery (``/v3/paths/list``, ``/v3/config/global/get``) it answers
what Quality needs, as a kit Device on MediaMTX 1.21.2 does (checked on a
test-pattern Device, #48):

- ``GET /v3/config/paths/get/{name}``: the path's config, or 404 "path not found";
- ``PATCH /v3/config/paths/patch/{name}``: merges the body's fields into that
  config and answers ``{"status": "ok"}``, or 404 for a path with no config.
  Unlike MediaMTX it does not reject unknown field names;
- ``GET /v3/info``: ``version`` and ``started``, which only a restart changes.

Tests steer it through ``/_script/...`` (wrapped by ``ScriptedDevice``):
every PATCH received, whatever it answered; every path config read; a restart that puts every path
config back as scripted and moves ``started``; and a scripted failure for
every PATCH.

Its clock is the host's plus the whole seconds in ``clock-offset`` next to
the script, if that file exists: the ``Date`` header every response carries
is read from it per request, so a test can skew the Device's clock, or fix
it, while the Device is added. It accepts any credentials and serves no media.

A script with a ``"sidecar"`` entry also runs a hostile GPS Sidecar on 9998:

    "sidecar": {"lines": [<base64 of each line>], "cert": <path>, "key": <path>}

Every GET there answers ``200 application/x-ndjson``, chunked, as the kit's
Sidecar does (#54), and then sends the scripted lines byte for byte, each
followed by a newline, over and over with a short pause between rounds, until
the client hangs up. Lines are base64 in the script so a test can send bytes
that are not text. The certificate is the Device's own, or an impostor's
from a second ``gen-cert.sh`` run for the same label. ``/_script/sidecar``
on 9997 reports every request the Sidecar read (path and ``Authorization``)
and how many connections it accepted, counted before the TLS handshake, so
a test can tell "never asked" from "asked, but refused before any request".
"""

import base64
import datetime
import http.server
import json
import os
import ssl
import sys
import threading
import time

CERT, KEY, SCRIPT = sys.argv[1:4]
CLOCK_OFFSET = os.path.join(os.path.dirname(SCRIPT), "clock-offset")

with open(SCRIPT) as fh:
    scripted = json.load(fh)


def now_rfc3339() -> str:
    """The current time as MediaMTX writes ``started``: UTC, nanoseconds, a ``Z``."""
    ns = time.time_ns()
    whole = datetime.datetime.fromtimestamp(ns // 1_000_000_000, datetime.UTC)
    return f"{whole:%Y-%m-%dT%H:%M:%S}.{ns % 1_000_000_000:09d}Z"


def boot_configs() -> dict:
    return json.loads(json.dumps(scripted.get("path_configs", {})))


lock = threading.Lock()
state = {
    "configs": boot_configs(),
    "started": scripted.get("started") or now_rfc3339(),
    "patches": [],
    "config_reads": [],  # the path name of every config GET, in order
    "fail": None,  # {"status": int, "body": {...}} answers every PATCH while set
    "sidecar_requests": [],  # {"path", "authorization"} of every request the Sidecar read
    "sidecar_connections": 0,  # every connection the Sidecar accepted, before its handshake
}

ROUTES = {
    "/v3/paths/list": {"pageCount": 1, "itemCount": len(scripted["paths"]), "items": scripted["paths"]},
    "/v3/config/global/get": scripted.get("global", {"rtspsAddress": ":8322"}),
}
CONFIG_GET = "/v3/config/paths/get/"
CONFIG_PATCH = "/v3/config/paths/patch/"
NOT_FOUND = {"status": "error", "error": "path not found"}


def clock_offset():
    try:
        with open(CLOCK_OFFSET) as fh:
            return int(fh.read())
    except FileNotFoundError:
        return 0


class Handler(http.server.BaseHTTPRequestHandler):
    def date_time_string(self, timestamp=None):  # what send_response puts in Date
        return super().date_time_string(time.time() + clock_offset() if timestamp is None else timestamp)

    def do_GET(self):  # http.server's naming
        path = self.path.split("?", 1)[0]
        with lock:
            if path == "/v3/info":
                return self.answer(200, {"version": "v1.21.2", "started": state["started"]})
            if path.startswith(CONFIG_GET):
                state["config_reads"].append(path.removeprefix(CONFIG_GET))
                config = state["configs"].get(path.removeprefix(CONFIG_GET))
                return self.answer(200, config) if config is not None else self.answer(404, NOT_FOUND)
            if path == "/_script/patches":
                return self.answer(200, state["patches"])
            if path == "/_script/config-reads":
                return self.answer(200, state["config_reads"])
            if path == "/_script/sidecar":
                return self.answer(200, {"requests": state["sidecar_requests"],
                                         "connections": state["sidecar_connections"]})
        body = ROUTES.get(path)
        if body is None:
            return self.answer(404, None)
        self.answer(200, body)

    def do_PATCH(self):
        path = self.path.split("?", 1)[0]
        if not path.startswith(CONFIG_PATCH):
            return self.answer(404, None)
        try:
            body = self.read_json()
        except ValueError as exc:
            return self.answer(400, {"status": "error", "error": str(exc)})
        name = path.removeprefix(CONFIG_PATCH)
        with lock:
            state["patches"].append({"path": name, "body": body})
            if state["fail"] is not None:
                return self.answer(state["fail"]["status"], state["fail"]["body"])
            if name not in state["configs"]:
                return self.answer(404, NOT_FOUND)
            state["configs"][name].update(body)
        self.answer(200, {"status": "ok"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self.read_json()
        with lock:
            if path == "/_script/restart":
                state["configs"] = boot_configs()
                state["started"] = body.get("started") or now_rfc3339()
            elif path == "/_script/fail-patches":
                state["fail"] = body if body.get("status") is not None else None
            else:
                return self.answer(404, None)
        self.answer(200, {"status": "ok"})

    def read_json(self):
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.loads(data or b"{}")
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        return body

    def answer(self, status, body):
        self.send_response(status)
        if body is None:
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


SIDECAR_ROUND_PAUSE = 0.2  # seconds between rounds of the scripted lines


class SidecarHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # for chunked transfer, as the kit's Sidecar streams

    def do_GET(self):  # http.server's naming
        with lock:
            state["sidecar_requests"].append({"path": self.path, "authorization": self.headers.get("Authorization")})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                for line in SIDECAR_LINES:
                    data = line + b"\n"
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                time.sleep(SIDECAR_ROUND_PAUSE)
        except OSError:  # the client hung up
            return

    def log_message(self, *args):
        pass


class SidecarServer(http.server.ThreadingHTTPServer):
    """Counts each connection, then shakes hands in that connection's own thread, so a client
    that aborts the handshake (a pin that does not match) is counted and blocks no one."""

    daemon_threads = True

    def finish_request(self, request, client_address):
        with lock:
            state["sidecar_connections"] += 1
        try:
            tls = sidecar_context.wrap_socket(request, server_side=True)
        except OSError:  # ssl.SSLError included: the client refused the certificate
            return
        try:
            self.RequestHandlerClass(tls, client_address, self)
        finally:
            tls.close()


sidecar = scripted.get("sidecar")
if sidecar is not None:
    SIDECAR_LINES = [base64.b64decode(line) for line in sidecar["lines"]]
    sidecar_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sidecar_context.load_cert_chain(sidecar["cert"], sidecar["key"])
    sidecar_server = SidecarServer(("0.0.0.0", 9998), SidecarHandler)
    threading.Thread(target=sidecar_server.serve_forever, daemon=True).start()

server = http.server.ThreadingHTTPServer(("0.0.0.0", 9997), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(CERT, KEY)
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
