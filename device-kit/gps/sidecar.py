"""The Device Kit's GPS Sidecar: NMEA from a GNSS receiver, served as JSON fixes.

Runs on the stock python:3.13-slim image with the standard library only (see
compose.yaml beside it). It reads RMC, GGA and VTG sentences from the
receiver at /dev/gps, or loops a recorded track with GPS_SOURCE=replay:<file>,
and serves one endpoint over HTTPS with the Device's certificate and HTTP
Basic auth as the Device's instance user:

    GET /v1/fixes?interval=<seconds>

The answer is a stream of newline-delimited JSON, one fix per line, sent as
the fix arrives from the receiver but never sooner than ``interval`` after
the line before. A silent receiver sends nothing, so no line is ever an old
fix sent again. Each line carries:

    {"time": "2026-10-15T09:00:00.000Z", "lat": 51.5, "lon": -0.125,
     "alt": 35.0, "speed": 5.0, "track": 12.5, "mode": "3d",
     "satellites": 9, "hdop": 0.8}

time is GPS UTC, lat and lon decimal degrees, alt metres above sea level,
speed metres per second, track degrees true, mode "none", "2d" or "3d", and
any field the receiver did not provide is null.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import json
import os
import re
import secrets
import select
import signal
import ssl
import sys
import termios
import threading
import time
import tty
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PORT = 9998
CERT = "/certs/server.crt"
KEY = "/certs/server.key"
RECEIVER = "/dev/gps"
TRACKS = "/gps/tracks"
ENDPOINT = "/v1/fixes"

MAX_LINE = 1024  # bytes; a longer line is dropped whole
QUIET_SECONDS = 0.1  # a pause this long ends a receiver's burst of sentences
FRESH_SECONDS = 1.0  # a fix older than this when its line falls due is not sent
INTERVAL_MIN, INTERVAL_MAX, INTERVAL_DEFAULT = 0.1, 3600.0, 1.0
SOCKET_TIMEOUT = 10  # handshake, request, and each write to a slow client
MAX_TRACK_BYTES = 16 * 1024 * 1024
KNOT = 1852 / 3600  # metres per second


class Fatal(Exception):
    """A problem a person must fix; the Sidecar exits with this message."""


# -- NMEA ---------------------------------------------------------------------

SENTENCE = re.compile(rb"^\$([A-Z]{2})([A-Z]{3}),([\x20-\x23\x25-\x29\x2b-\x7e]*)\*([0-9A-Fa-f]{2})$")
UNSIGNED = re.compile(r"^\d+(\.\d+)?$")
SIGNED = re.compile(r"^-?\d+(\.\d+)?$")
UTC = re.compile(r"^(\d{2})(\d{2})(\d{2}(?:\.\d+)?)$")
DATE = re.compile(r"^(\d{2})(\d{2})(\d{2})$")


def sentence(line: bytes) -> tuple[str, list[str]] | None:
    """A checked sentence as (type, fields), or None for anything else,
    including a bad or missing checksum."""
    match = SENTENCE.match(line)
    if not match:
        return None
    checksum = 0
    for byte in line[1:line.rindex(b"*")]:
        checksum ^= byte
    if checksum != int(match[4], 16):
        return None
    return match[2].decode(), match[3].decode().split(",")


def field(fields: list[str], index: int) -> str:
    return fields[index] if index < len(fields) else ""


def number(text: str, *, signed: bool = False) -> float | None:
    return float(text) if (SIGNED if signed else UNSIGNED).match(text) else None


def integer(text: str) -> int | None:
    return int(text) if text.isdigit() else None


def angle(text: str) -> float | None:
    value = number(text)
    return value if value is not None and value <= 360 else None


def coordinate(text: str, hemisphere: str, degree_digits: int, limit: int) -> float | None:
    match = re.match(rf"^(\d{{{degree_digits}}})(\d{{2}}(?:\.\d+)?)$", text)
    if not match or hemisphere not in ("N", "S", "E", "W"):
        return None
    minutes = float(match[2])
    value = int(match[1]) + minutes / 60
    if minutes >= 60 or value > limit:
        return None
    return round(-value if hemisphere in ("S", "W") else value, 7)


def position(fields: list[str], at: int) -> tuple[float, float] | None:
    lat = coordinate(field(fields, at), field(fields, at + 1), 2, 90)
    lon = coordinate(field(fields, at + 2), field(fields, at + 3), 3, 180)
    return (lat, lon) if lat is not None and lon is not None else None


def utc_seconds(text: str) -> float | None:
    match = UTC.match(text)
    if not match:
        return None
    hours, minutes, seconds = int(match[1]), int(match[2]), float(match[3])
    if hours > 23 or minutes > 59 or seconds >= 61:
        return None
    return hours * 3600 + minutes * 60 + seconds


def utc_date(text: str) -> datetime.date | None:
    match = DATE.match(text)
    if not match:
        return None
    try:
        return datetime.date(2000 + int(match[3]), int(match[2]), int(match[1]))
    except ValueError:
        return None


def parse_rmc(fields: list[str]) -> dict:
    knots = number(field(fields, 6))
    return {"status": field(fields, 1), "position": position(fields, 2),
            "speed": None if knots is None else knots * KNOT, "track": angle(field(fields, 7)),
            "date": utc_date(field(fields, 8))}


def parse_gga(fields: list[str]) -> dict:
    return {"position": position(fields, 1), "quality": integer(field(fields, 5)),
            "satellites": integer(field(fields, 6)), "hdop": number(field(fields, 7)),
            "alt": number(field(fields, 8), signed=True) if field(fields, 9) in ("M", "") else None}


def parse_vtg(fields: list[str]) -> dict:
    kmh, knots = number(field(fields, 6)), number(field(fields, 4))
    speed = kmh / 3.6 if kmh is not None else None if knots is None else knots * KNOT
    return {"track": angle(field(fields, 0)), "speed": speed}


PARSERS = {"RMC": parse_rmc, "GGA": parse_gga, "VTG": parse_vtg}


def build_fix(epoch: dict, seconds: float | None) -> dict:
    rmc, gga, vtg = epoch.get("RMC", {}), epoch.get("GGA", {}), epoch.get("VTG", {})
    where = gga.get("position") or rmc.get("position")
    if where is None or rmc.get("status", "A") != "A" or gga.get("quality", 1) in (0, None):
        mode = "none"
    elif gga.get("alt") is not None:
        mode = "3d"
    else:
        mode = "2d"
    when = None
    if seconds is not None and rmc.get("date") is not None:
        stamp = datetime.datetime.combine(rmc["date"], datetime.time(), datetime.UTC)
        stamp += datetime.timedelta(seconds=seconds)
        when = stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"
    speed = rmc.get("speed") if rmc.get("speed") is not None else vtg.get("speed")
    track = rmc.get("track") if rmc.get("track") is not None else vtg.get("track")
    return {
        "time": when,
        "lat": where[0] if where else None,
        "lon": where[1] if where else None,
        "alt": gga.get("alt"),
        "speed": None if speed is None else round(speed, 3),
        "track": track,
        "mode": mode,
        "satellites": gga.get("satellites"),
        "hdop": gga.get("hdop"),
    }


class Lines:
    """Splits a byte stream into lines, dropping any line over MAX_LINE."""

    def __init__(self):
        self._buffer = bytearray()
        self._overlong = False

    def feed(self, data: bytes) -> list[bytes]:
        lines = []
        while data:
            end = data.find(b"\n")
            part, data = (data, b"") if end < 0 else (data[:end], data[end + 1:])
            if not self._overlong:
                self._buffer += part
                if len(self._buffer) > MAX_LINE + 1:  # +1 for a trailing \r
                    self._overlong, self._buffer = True, bytearray()
            if end >= 0:
                line = bytes(self._buffer).rstrip(b"\r")
                if not self._overlong and len(line) <= MAX_LINE:
                    lines.append(line)
                self._overlong, self._buffer = False, bytearray()
        return lines


class Fixes:
    """Assembles each epoch's sentences into one fix and holds the latest.

    A receiver sends a burst of sentences per epoch (RMC, VTG, GGA... in any
    order, VTG with no time of its own). The epoch ends when a sentence
    carries a different time, or when the receiver pauses (``flush``)."""

    def __init__(self):
        self.changed = threading.Condition()
        self.latest: dict | None = None
        self.count = 0
        self.received_at = 0.0
        self._epoch: dict = {}
        self._seconds: float | None = None

    def add(self, kind: str, fields: list[str]) -> None:
        if kind not in PARSERS:
            return
        seconds = utc_seconds(field(fields, 0)) if kind != "VTG" else None
        if seconds is not None and seconds != self._seconds:
            self.flush()
            self._seconds = seconds
        self._epoch[kind] = PARSERS[kind](fields)

    def flush(self) -> None:
        if "RMC" in self._epoch or "GGA" in self._epoch:
            fix = build_fix(self._epoch, self._seconds)
            with self.changed:
                self.latest, self.count, self.received_at = fix, self.count + 1, time.monotonic()
                self.changed.notify_all()
        self._epoch = {}


def read_into(source: Iterator[bytes | None], fixes: Fixes) -> None:
    """Feed every checked sentence from ``source`` to ``fixes``; None means
    the source paused."""
    lines = Lines()
    for chunk in source:
        if chunk is None:
            fixes.flush()
            continue
        for line in lines.feed(chunk):
            parsed = sentence(line)
            if parsed:
                fixes.add(*parsed)


# -- sources --------------------------------------------------------------------

BAUD_RATES = {rate: getattr(termios, f"B{rate}") for rate in
              (4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800) if hasattr(termios, f"B{rate}")}
HINT = ("Check the receiver is plugged in and GPS_TTY in .env names its /dev/serial/by-id/ path "
        "(../gen-cert.sh finds it), or set GPS_SOURCE=replay:sample.nmea to replay a track instead.")


def open_receiver(host_path: str, baud: str) -> Iterator[bytes | None]:
    """Open the receiver now, so a missing one fails before the Sidecar
    listens; then yield what it sends, and None after each pause."""
    if not host_path:
        raise Fatal(f"GPS_TTY is not set, so there is no receiver to read. {HINT}")
    try:
        fd = os.open(RECEIVER, os.O_RDONLY | os.O_NOCTTY)
    except OSError as exc:
        raise Fatal(f"cannot read GPS_TTY {host_path}: {exc.strerror}. {HINT}") from exc
    if not os.isatty(fd):
        os.close(fd)
        raise Fatal(f"GPS_TTY {host_path} is not a serial device. {HINT}")
    tty.setraw(fd)  # no echo back to the receiver, no line editing
    if baud:
        if not baud.isdigit() or int(baud) not in BAUD_RATES:
            raise Fatal(f"GPS_BAUD {baud!r} is not one of {', '.join(map(str, BAUD_RATES))}.")
        attributes = termios.tcgetattr(fd)
        attributes[4] = attributes[5] = BAUD_RATES[int(baud)]
        termios.tcsetattr(fd, termios.TCSANOW, attributes)

    def chunks() -> Iterator[bytes | None]:
        while True:
            ready, _, _ = select.select([fd], [], [], QUIET_SECONDS)
            if not ready:
                yield None
                continue
            try:
                data = os.read(fd, 4096)
            except OSError as exc:
                raise Fatal(f"GPS_TTY {host_path} stopped answering ({exc.strerror}). {HINT}") from exc
            if not data:
                raise Fatal(f"GPS_TTY {host_path} closed. {HINT}")
            yield data

    return chunks()


def open_track(name: str) -> Iterator[bytes | None]:
    """Load a recorded NMEA track now; then yield it one epoch at a time,
    at the pace its own times set, forever."""
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", name):
        raise Fatal(f"GPS_SOURCE=replay:{name} must name a file in the kit's gps/tracks folder.")
    path = os.path.join(TRACKS, name)
    try:
        with open(path, "rb") as track:
            data = track.read(MAX_TRACK_BYTES + 1)
    except OSError as exc:
        raise Fatal(f"cannot read the replay track gps/tracks/{name}: {exc.strerror}.") from exc
    if len(data) > MAX_TRACK_BYTES:
        raise Fatal(f"the replay track gps/tracks/{name} is over {MAX_TRACK_BYTES // 2**20} MB.")

    epochs: list[tuple[float, bytes]] = []
    block, seconds = bytearray(), None
    for raw in data.splitlines():
        parsed = sentence(raw.rstrip(b"\r")) if len(raw) <= MAX_LINE + 1 else None
        stamp = utc_seconds(field(parsed[1], 0)) if parsed and parsed[0] in ("RMC", "GGA") else None
        if stamp is not None and stamp != seconds:
            if seconds is not None:
                epochs.append((seconds, bytes(block)))
                block = bytearray()
            seconds = stamp
        block += raw + b"\n"  # every malformed line too: the reader must drop it
    if seconds is None:
        raise Fatal(f"the replay track gps/tracks/{name} has no RMC or GGA sentence with a time.")
    epochs.append((seconds, bytes(block)))

    def gap(after: float, before: float) -> float:
        step = (before - after) % 86400  # a track may run past midnight
        return step if 0 < step <= 60 else 1.0

    loop_gap = gap(epochs[0][0], epochs[1][0]) if len(epochs) > 1 else 1.0

    def chunks() -> Iterator[bytes | None]:
        due = time.monotonic()
        while True:
            for index, (stamp, lines) in enumerate(epochs):
                yield lines
                yield None
                due += gap(stamp, epochs[index + 1][0]) if index + 1 < len(epochs) else loop_gap
                time.sleep(max(0.0, due - time.monotonic()))

    return chunks()


# -- HTTPS ----------------------------------------------------------------------

class Server(ThreadingHTTPServer):
    """One thread per client; TLS is negotiated in that thread, so a client
    that stalls its handshake holds up nobody else."""

    daemon_threads = True

    def __init__(self, context: ssl.SSLContext, fixes: Fixes, username: str, password: str):
        super().__init__(("", PORT), Handler)
        self.context, self.fixes = context, fixes
        self.credentials = f"{username}:{password}".encode()

    def finish_request(self, request, client_address) -> None:
        request.settimeout(SOCKET_TIMEOUT)
        try:
            connection = self.context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        try:
            self.RequestHandlerClass(connection, client_address, self)
        finally:
            connection.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "gps-sidecar"
    sys_version = ""
    server: Server

    def do_GET(self) -> None:
        if not self._authorized():
            return self._refuse(401, "authentication required")
        url = urlsplit(self.path)
        if url.path != ENDPOINT:
            return self._refuse(404, "not found")
        values = parse_qs(url.query).get("interval", [str(INTERVAL_DEFAULT)])
        interval = number(values[-1])
        if interval is None or not INTERVAL_MIN <= interval <= INTERVAL_MAX:
            return self._refuse(400, f"interval must be seconds from {INTERVAL_MIN} to {INTERVAL_MAX:g}")
        self._stream(interval)

    def _authorized(self) -> bool:
        scheme, _, encoded = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            given = base64.b64decode(encoded.strip(), validate=True)
        except (binascii.Error, ValueError):
            return False
        return secrets.compare_digest(given, self.server.credentials)

    def _refuse(self, status: int, message: str) -> None:
        body = f"{message}\n".encode()
        self.send_response(status)
        if status == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="gps"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _stream(self, interval: float) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.close_connection = True
        fixes, sent, due = self.server.fixes, 0, time.monotonic()
        try:
            while True:
                with fixes.changed:
                    while True:
                        now = time.monotonic()
                        if now < due:
                            fixes.changed.wait(due - now)
                        elif fixes.count > sent and fixes.received_at >= due - FRESH_SECONDS:
                            break
                        else:
                            fixes.changed.wait()
                    fix, sent = fixes.latest, fixes.count
                line = json.dumps(fix, allow_nan=False).encode() + b"\n"
                self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                self.wfile.flush()
                due = time.monotonic() + interval
        except OSError:
            pass  # the client went away, or stopped reading

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write(f"{self.address_string()} {format % args}\n")


def tls_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(CERT, KEY)
    except (OSError, ssl.SSLError) as exc:
        raise Fatal(f"cannot load the Device certificate from ./certs ({exc}). Run ../gen-cert.sh first.") from exc
    return context


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # PID 1 gets no default handler
    username, password = os.environ.get("INSTANCE_USER", ""), os.environ.get("INSTANCE_PASS", "")
    if not username or not password:
        raise Fatal("INSTANCE_USER and INSTANCE_PASS must be set. Run ../gen-cert.sh first.")
    context = tls_context()
    setting = os.environ.get("GPS_SOURCE", "").strip() or "tty"
    if setting.startswith("replay:"):
        source, what = open_track(setting.removeprefix("replay:")), f"replaying gps/tracks/{setting[7:]}"
    elif setting == "tty":
        tty_path = os.environ.get("GPS_TTY", "").strip()
        source, what = open_receiver(tty_path, os.environ.get("GPS_BAUD", "").strip()), f"reading {tty_path}"
    else:
        raise Fatal(f"GPS_SOURCE must be tty or replay:<file>, not {setting!r}.")
    fixes = Fixes()
    server = Server(context, fixes, username, password)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"GPS Sidecar: {what}; serving https://0.0.0.0:{PORT}{ENDPOINT}", file=sys.stderr)
    read_into(source, fixes)


if __name__ == "__main__":
    try:
        main()
    except Fatal as exc:
        print(f"GPS Sidecar: {exc}", file=sys.stderr)
        sys.exit(1)
