"""Browser contexts cut off from everything but the instance, to prove the UI
works on a monitoring station with no internet (#53, story 15 and 21).

``Offline`` routes every request a Playwright context makes. A request to one
of the allowed origins (each instance's ``base_url``) goes ahead; anything
else is aborted as if the network were down and noted in ``blocked``, so a
test can assert that nothing tried to leave. Seam B's ``offline`` fixture
(``tests/seam_b/conftest.py``) wraps the shared instance; build one by hand
for another instance or several.

``stand_in`` answers one outside origin locally instead, for a test of a
feature that talks to the internet on purpose (the online tile server,
#62): its requests are fulfilled in the browser and listed, never sent, and
never count as blocked. Every other origin stays blocked, so the proof that
nothing else left holds unchanged.

Routing sees HTTP(S) requests only. WebRTC media (WHEP's ICE over UDP) is not
routed, which is fine: its candidates are the Relay's own. Routing also
disables the browser's HTTP cache for the context, so every load is fetched.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page, Route

from harness.instance import Instance


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class Offline:
    """A browser context in which only the given instances are reachable."""

    def __init__(self, context: BrowserContext, *instances: Instance):
        self.context = context
        self.blocked: list[str] = []
        self._allowed = frozenset(origin_of(instance.base_url) for instance in instances)
        context.route("**/*", self._route)

    def new_page(self, **kwargs: Any) -> Page:
        return self.context.new_page(**kwargs)

    def stand_in(self, origin: str, body: bytes, content_type: str,
                 missing: Callable[[str], bool] | None = None) -> list[str]:
        """Answer every request to ``origin`` (``scheme://host[:port]``) with ``body`` in the
        browser, as a server allowing any origin would, so nothing is sent; a URL ``missing``
        returns true for is answered 404 instead, as a server without that file would. Returns
        the list the URLs answered are appended to, 404s included. Routes registered later run
        first, so this one is asked before ``_route``, and a request to any other origin still
        reaches it and is blocked."""
        answered: list[str] = []
        origin = origin_of(origin)

        def answer(route: Route) -> None:
            if origin_of(route.request.url) != origin:
                route.fallback()
                return
            answered.append(route.request.url)
            headers = {"access-control-allow-origin": "*"}
            if missing and missing(route.request.url):
                route.fulfill(status=404, body=b"", headers=headers)
                return
            route.fulfill(status=200, body=body, content_type=content_type, headers=headers)

        self.context.route(f"{origin}/**", answer)
        return answered

    def _route(self, route: Route) -> None:
        url = route.request.url
        if origin_of(url) in self._allowed:
            route.fallback()
            return
        self.blocked.append(url)
        route.abort("internetdisconnected")

    def close(self) -> None:
        """Leave each page as a student leaves a tab (pagehide ends its WHEP
        sessions; see ``tests/conftest.py::page``), then close the context."""
        for page in list(self.context.pages):
            if not page.is_closed():
                page.goto("about:blank")
        self.context.close()
