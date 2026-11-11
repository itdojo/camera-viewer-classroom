"""Browsers that are already logged in, so browser tests start past the login wall.

``LoggedInBrowser`` wraps pytest-playwright's ``Browser``. Every context it
opens (``new_context``, ``new_page``, and so the ``context`` and ``page``
fixtures) gets a fresh session cookie for every instance of the pytest
session, and contexts already open get one for each instance started later.
Each instance names its cookie after its own secret, so the cookies of
several instances on 127.0.0.1 (cookies ignore the port) live side by side.

A test of the login wall itself starts logged out with
``page.context.clear_cookies()``.

Every context it opens also notes what went wrong in the browser (console
errors, uncaught page errors, failed requests, HTTP errors) in
``diagnostics``, which Seam B prints when a test fails: a page whose scripts
never ran looks, to an assertion, just like a page waiting for data. A page
whose script fails to load reloads itself, a few times at most; see
``RELOAD_ON_SCRIPT_FAILURE``. A ``goto`` or ``reload`` the network change
aborts is tried again the same number of times; see ``retry_network_changed``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
from playwright.sync_api import Browser, BrowserContext, Error, Page

from harness.instance import Instance, InstancePool

# Where management-bar.js saves this browser's Management Bar layout.
MANAGEMENT_BAR_STORAGE_KEY = "camera-viewer:management-bar"

# Runs before every page's own scripts. Storage can throw (about:blank, an
# opaque origin), and then there is nothing to seed. No `order` is seeded, so
# the Panels keep their default order whatever ids they have.
SEED_MANAGEMENT_BAR_OPEN = f"""
try {{
    if (localStorage.getItem({json.dumps(MANAGEMENT_BAR_STORAGE_KEY)}) === null) {{
        localStorage.setItem({json.dumps(MANAGEMENT_BAR_STORAGE_KEY)}, JSON.stringify({{ open: true, collapsed: [] }}));
    }}
}} catch (error) {{}}
"""

# Runs before every page's own scripts. On the Linux CI host each Device's
# Docker network adds interfaces and addresses, and Chromium aborts whatever
# is in flight at that moment with net::ERR_NETWORK_CHANGED. When that is a
# module the page imports, main.js never runs and the page stays static HTML.
# A failed <script> fires `error` (captured here, as it does not bubble), and
# the page reloads, at most RELOADS times per tab so a script that is really
# missing still fails the test. The console error lands in `diagnostics`.
RELOADS = 3
RELOAD_ON_SCRIPT_FAILURE = f"""
(() => {{
    const key = 'camera-viewer-test:script-reloads';
    window.addEventListener('error', (event) => {{
        if (!(event.target instanceof HTMLScriptElement)) return;
        let reloads = 0;
        try {{ reloads = Number(sessionStorage.getItem(key)) || 0; }} catch (error) {{ return; }}
        if (reloads >= {RELOADS}) return;
        sessionStorage.setItem(key, String(reloads + 1));
        console.error(`harness: ${{event.target.src}} failed to load; reloading (${{reloads + 1}} of {RELOADS})`);
        location.reload();
    }}, true);
}})();
"""

# The same network change can abort the navigation itself, and then
# `page.goto` raises before any page script runs (#98). A pause lets the
# change settle first, so the retries don't all land inside it (unmeasured).
NETWORK_CHANGED = "net::ERR_NETWORK_CHANGED"
RETRY_PAUSE_SECONDS = 1.0


def retry_network_changed(navigate: Callable[..., Any], note: Callable[[str], None]) -> Callable[..., Any]:
    """Wrap a page's ``goto`` or ``reload`` so a call the network change
    aborts is made again with the same arguments, RELOADS attempts in all,
    each retry noted. Any other error, and the last attempt's, raises as is.
    Only the message's first line counts, since a timeout's call log below
    it may quote an earlier abort."""
    def navigate_again(*args: Any, **kwargs: Any) -> Any:
        for attempt in range(1, RELOADS + 1):
            try:
                return navigate(*args, **kwargs)
            except Error as error:
                if NETWORK_CHANGED not in error.message.partition("\n")[0] or attempt == RELOADS:
                    raise
                note(f"harness: {navigate.__name__} {' '.join(map(str, args))} hit {NETWORK_CHANGED};"
                     f" retrying ({attempt} of {RELOADS - 1})")
                time.sleep(RETRY_PAUSE_SECONDS)
    return navigate_again


# What a failing test's page looked like from inside: a bar never marked
# ready means main.js stopped before mountManagementBar finished.
PAGE_STATE = f"""() => {{
    const bar = document.querySelector('.management-bar');
    let saved;
    try {{ saved = localStorage.getItem({json.dumps(MANAGEMENT_BAR_STORAGE_KEY)}); }} catch (error) {{ saved = String(error); }}
    return JSON.stringify({{
        readyState: document.readyState,
        barReady: bar?.hasAttribute('data-ready') ?? null,
        barOpen: bar?.hasAttribute('data-open') ?? null,
        savedBar: saved,
        diskShown: !document.querySelector('[data-disk]')?.hidden,
    }});
}}"""


class LoggedInBrowser:
    def __init__(self, browser: Browser, pool: InstancePool):
        self._browser = browser
        self._pool = pool
        self._contexts: list[BrowserContext] = []
        self.diagnostics: list[str] = []
        pool.on_start(self._log_in_everywhere)

    def new_context(self, *, seed_management_bar: bool = True, **kwargs: Any) -> BrowserContext:
        """Open a logged-in context. Unless ``seed_management_bar`` is false,
        its pages start with the Management Bar open and every Panel expanded.

        The bar starts closed for a real visitor, which hides the Camera,
        Capture and Device lists and the add forms that most browser tests
        drive. Seeding the saved bar state here, before any page script runs,
        keeps those tests about their own feature instead of each opening the
        bar first. It only fills an empty slot, so a test that closes the bar
        or collapses a Panel still sees that after a reload. Management Bar
        tests pass ``seed_management_bar=False`` to start from clean storage.
        """
        # Instances serve HTTPS with a certificate they made themselves, which a
        # person accepts once by hand; a test context accepts it the same way.
        kwargs.setdefault("ignore_https_errors", True)
        context = self._browser.new_context(**kwargs)
        context.add_init_script(RELOAD_ON_SCRIPT_FAILURE)
        if seed_management_bar:
            context.add_init_script(SEED_MANAGEMENT_BAR_OPEN)
        self._watch(context)
        context.on("page", self._retry_navigation)
        for page in context.pages:
            self._retry_navigation(page)
        self._contexts.append(context)
        context.on("close", self._forget)
        for instance in list(self._pool.instances.values()):
            self._log_in(context, instance)
        return context

    def new_page(self, **kwargs: Any) -> Page:
        return self.new_context(**kwargs).new_page()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._browser, name)

    def _log_in_everywhere(self, instance: Instance) -> None:
        for context in list(self._contexts):
            self._log_in(context, instance)

    @staticmethod
    def _log_in(context: BrowserContext, instance: Instance) -> None:
        try:
            cookies = instance.browser_cookies()
        except (httpx.HTTPError, RuntimeError, IndexError, AssertionError):
            return  # a stopped instance: nothing to log in to
        context.add_cookies(cookies)

    def page_states(self) -> list[str]:
        """One line per open page: whether main.js mounted the Management Bar
        (``data-ready``), the bar layout it saved, and whether the disk
        indicator has had data. Read while the test's pages are still open."""
        lines = []
        for context in self._contexts:
            for page in context.pages:
                try:
                    state = page.evaluate(PAGE_STATE)
                except Exception as error:  # a closed or crashed page says so
                    state = f"unreadable ({error.__class__.__name__})"
                lines.append(f"page {page.url}: {state}")
        return lines

    def _note(self, line: str) -> None:
        self.diagnostics.append(f"{time.strftime('%H:%M:%S')} {line}")

    def _retry_navigation(self, page: Page) -> None:
        # The `page` event reaches this before new_page() returns, on the
        # object it returns, so every page of the context is covered.
        page.goto = retry_network_changed(page.goto, self._note)
        page.reload = retry_network_changed(page.reload, self._note)

    def _watch(self, context: BrowserContext) -> None:
        note = self._note

        def console(message) -> None:
            if message.type == "error":
                note(f"console error: {message.text} ({message.location.get('url', '')})")

        def response(response) -> None:
            if response.status >= 400:
                note(f"HTTP {response.status}: {response.request.method} {response.url}")

        context.on("console", console)
        context.on("weberror", lambda error: note(f"page error: {error.error}"))
        context.on("requestfailed", lambda request: note(
            f"request failed: {request.method} {request.url}: {request.failure}"))
        context.on("response", response)

    def _forget(self, context: BrowserContext) -> None:
        if context in self._contexts:
            self._contexts.remove(context)
