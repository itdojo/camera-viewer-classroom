"""Auth: one shared password per instance, and a session cookie in front of everything.

Deny by default. ``SessionGate`` wraps the whole ASGI app, so every route,
the event stream, the WHEP proxy and every static file need a valid session,
including routes added later, which need do nothing to be protected. The only
exceptions are ``PUBLIC``: the login endpoint, the login page's own files,
and the Relay's segment hook (which checks a token of its own).
Without a session an ``/api/`` request gets 401 ``unauthenticated`` and
anything else is redirected to ``/login``.

A session is a signed, expiring token in an HttpOnly, SameSite=Strict cookie.
The signing key is derived from the password and a random secret kept in the
config folder (mode 0600), so sessions survive a restart of the backend or
the container, and changing ``CV_PASSWORD`` ends every session. The cookie's
name carries a short id of that secret, so two instances on one host (cookies
ignore the port) never overwrite each other's session. Logging out revokes
the token in memory until it would have expired anyway.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

SECRET_FILE = "session-secret"

# Reachable without a session: logging in, and the login page with the files it loads.
PUBLIC = frozenset({
    "/api/session",
    "/login",
    "/css/tokens.css",
    "/css/app.css",
    "/js/login.js",
    # The bundled fonts (#63), so the login page looks right with no internet.
    # The page can only load them from here, and they are OFL-licensed files
    # that reveal nothing about the instance.
    "/css/fonts.css",
    "/fonts/fira-sans/fira-sans-regular.woff2",
    "/fonts/fira-sans/fira-sans-medium.woff2",
    "/fonts/fira-sans/fira-sans-bold.woff2",
    "/fonts/fira-sans/fira-sans-italic.woff2",
    "/fonts/fira-sans/fira-sans-bold-italic.woff2",
    "/fonts/fira-code/fira-code-variable.woff2",
    # The Relay's recording hooks: not a browser, so no session. The route
    # checks its own per-process token instead (camera_viewer.api.recordings).
    "/internal/recorder/segments",
})


def load_secret(config_dir: Path) -> bytes:
    """The instance's session secret, created on first start. Falls back to a
    secret that lasts only this process if the config folder isn't writable."""
    path = config_dir / SECRET_FILE
    try:
        if path.exists():
            return bytes.fromhex(path.read_text().strip())
        secret = secrets.token_bytes(32)
        config_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(secret.hex())
        return secret
    except (OSError, ValueError) as exc:
        log.warning("cannot keep a session secret in %s (%s); sessions end when the backend restarts",
                    config_dir, exc)
        return secrets.token_bytes(32)


class LoginThrottle:
    """Slows down guessing the shared password, per client address.

    After ``max_failures`` wrong passwords in a row, the address is locked
    out for ``lockout_seconds``; each further lockout without a success in
    between doubles that, up to ``MAX_LOCKOUT_SECONDS``. While locked out
    every attempt is refused unchecked, the right password included, so
    guessing gains nothing. A right password clears the address's record.
    Kept in memory only; a restart forgives everyone.

    Behind one NAT, or Docker Desktop's port publishing, a whole class can
    share one address, so one student's typos can lock out everyone for a
    while: the defaults are lenient for that reason.
    """

    MAX_LOCKOUT_SECONDS = 15 * 60
    MAX_CLIENTS = 10_000  # records kept; the stalest are forgotten beyond this

    def __init__(self, max_failures: int, lockout_seconds: float, clock=time.monotonic):
        self._max_failures = max_failures
        self._lockout = lockout_seconds
        self._clock = clock
        self._clients: dict[str, list[float]] = {}  # address -> [failures, lockouts, locked until, last seen]

    def retry_after(self, client: str) -> float:
        """Seconds until ``client`` may try again; 0 if it may now."""
        record = self._clients.get(client)
        return max(record[2] - self._clock(), 0.0) if record else 0.0

    def failed(self, client: str) -> None:
        now = self._clock()
        record = self._clients.setdefault(client, [0, 0, 0.0, now])
        record[0] += 1
        record[3] = now
        if record[0] >= self._max_failures:
            record[0] = 0
            record[1] += 1
            record[2] = now + min(self._lockout * 2 ** (record[1] - 1), self.MAX_LOCKOUT_SECONDS)
            log.warning("login: %s locked out for %.0f s after repeated wrong passwords",
                        client, record[2] - now)
        if len(self._clients) > self.MAX_CLIENTS:
            for stale in sorted(self._clients, key=lambda c: self._clients[c][3])[:len(self._clients) // 10]:
                del self._clients[stale]

    def succeeded(self, client: str) -> None:
        self._clients.pop(client, None)


class Auth:
    """Checks the shared password and issues, verifies and revokes sessions."""

    def __init__(self, password: str, secret: bytes, max_age_seconds: int,
                 throttle: LoginThrottle | None = None):
        self.throttle = throttle or LoginThrottle(max_failures=10, lockout_seconds=30)
        self._password = password.encode()
        self._key = hmac.new(secret, b"session-key\0" + self._password, hashlib.sha256).digest()
        self.cookie_name = "cv_session_" + hashlib.sha256(b"cookie-name\0" + secret).hexdigest()[:8]
        self.max_age_seconds = max_age_seconds
        self._revoked: dict[str, float] = {}  # token -> its expiry

    def password_matches(self, attempt: str) -> bool:
        return hmac.compare_digest(attempt.encode(), self._password)

    def issue(self) -> str:
        expires = int(time.time()) + self.max_age_seconds
        payload = f"{expires}.{secrets.token_urlsafe(16)}"
        return f"{payload}.{self._sign(payload)}"

    def valid(self, token: str | None) -> bool:
        if not token or token in self._revoked:
            return False
        payload, _, signature = token.rpartition(".")
        if not payload or not hmac.compare_digest(signature, self._sign(payload)):
            return False
        expires, _, _ = payload.partition(".")
        return expires.isdigit() and int(expires) > time.time()

    def revoke(self, token: str | None) -> None:
        if not self.valid(token):
            return
        now = time.time()
        self._revoked = {t: exp for t, exp in self._revoked.items() if exp > now}
        self._revoked[token] = int(token.partition(".")[0])

    def token_of(self, conn: HTTPConnection) -> str | None:
        return conn.cookies.get(self.cookie_name)

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()


class SessionGate:
    """ASGI middleware: nothing but ``PUBLIC`` gets through without a session."""

    def __init__(self, app: ASGIApp, auth: Auth):
        self.app = app
        self.auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or scope["path"] in PUBLIC:
            await self.app(scope, receive, send)
            return
        if self.auth.valid(self.auth.token_of(HTTPConnection(scope))):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["path"].startswith("/api/"):
            body = json.dumps({"error": "unauthenticated", "message": "log in first"}).encode()
            headers = [(b"content-type", b"application/json")]
            status = 401
        else:
            body, headers, status = b"", [(b"location", b"/login")], 303
        await send({"type": "http.response.start", "status": status,
                    "headers": [*headers, (b"content-length", str(len(body)).encode()),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})
