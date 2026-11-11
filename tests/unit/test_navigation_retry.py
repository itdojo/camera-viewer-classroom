"""The harness's navigation retry (#98): a ``goto`` the Linux CI host's network
change aborts is made again, a bounded number of times, and nothing else is.

Playwright's ``route.abort`` has no network-changed code, so no browser test
can inject this error; a fake page whose navigations raise on cue stands in.
"""

import pytest
from playwright.sync_api import Error

from harness.browser import NETWORK_CHANGED, RELOADS, LoggedInBrowser, retry_network_changed

URL = "https://127.0.0.1:32768/"


class Navigation:
    """A ``goto`` and ``reload`` that raise each of ``errors`` in turn, then answer ``"response"``."""

    def __init__(self, *errors: Error):
        self.errors = list(errors)
        self.calls: list[tuple] = []

    def _navigate(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.errors:
            raise self.errors.pop(0)
        return "response"

    def goto(self, *args, **kwargs):
        return self._navigate(*args, **kwargs)

    def reload(self, *args, **kwargs):
        return self._navigate(*args, **kwargs)


class Pool:
    def on_start(self, listener):
        pass


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr("harness.browser.RETRY_PAUSE_SECONDS", 0)


def network_changed() -> Error:
    return Error(f"Page.goto: {NETWORK_CHANGED} at {URL}\nCall log:\n  - navigating to \"{URL}\"")


def test_a_navigation_the_network_change_aborts_is_made_again_with_the_same_arguments():
    navigation, notes = Navigation(network_changed(), network_changed()), []
    goto = retry_network_changed(navigation.goto, notes.append)

    assert goto(URL, wait_until="load") == "response"
    assert navigation.calls == [((URL,), {"wait_until": "load"})] * 3
    assert len(notes) == 2
    assert all(NETWORK_CHANGED in note and URL in note for note in notes)


def test_the_last_attempt_s_error_raises_once_the_attempts_are_spent():
    errors = [network_changed() for _ in range(RELOADS)]
    navigation, notes = Navigation(*errors), []
    goto = retry_network_changed(navigation.goto, notes.append)

    with pytest.raises(Error) as raised:
        goto(URL)
    assert raised.value is errors[-1]
    assert len(navigation.calls) == RELOADS
    assert len(notes) == RELOADS - 1


@pytest.mark.parametrize("message", [
    f"Page.goto: net::ERR_CONNECTION_REFUSED at {URL}",
    f"Timeout 30000ms exceeded.\nCall log:\n  - {NETWORK_CHANGED} on an earlier request",
])
def test_any_other_error_raises_at_once(message):
    other = Error(message)
    navigation, notes = Navigation(other), []
    goto = retry_network_changed(navigation.goto, notes.append)

    with pytest.raises(Error) as raised:
        goto(URL)
    assert raised.value is other
    assert len(navigation.calls) == 1
    assert notes == []


@pytest.mark.parametrize("method", ["goto", "reload"])
def test_every_page_of_a_logged_in_context_retries_both_navigations(method):
    browser, page = LoggedInBrowser(None, Pool()), Navigation(network_changed())
    browser._retry_navigation(page)

    assert getattr(page, method)() == "response"
    assert len(page.calls) == 2
    assert len(browser.diagnostics) == 1
