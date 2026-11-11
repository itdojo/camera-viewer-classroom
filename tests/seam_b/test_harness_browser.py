"""Seam B: the browser harness gets a page past a module that failed to load.

On the Linux CI host Chromium aborts in-flight requests with
net::ERR_NETWORK_CHANGED whenever a Device's Docker network comes or goes.
These tests stand that in with an aborted request for one module.
"""

from playwright.sync_api import expect

from harness.browser import RELOADS


def test_a_page_whose_module_failed_once_reloads_and_boots(browser, instance):
    page = browser.new_page()
    failed = []

    def abort_once(route):
        if failed:
            route.continue_()
        else:
            failed.append(route.request.url)
            route.abort("connectionreset")

    page.route("**/js/store.js", abort_once)
    page.goto(instance.base_url)
    expect(page.locator(".management-bar")).to_have_attribute("data-ready", "")
    assert len(failed) == 1


def test_a_module_that_never_loads_still_fails_the_page(browser, instance):
    page = browser.new_page()
    attempts = []

    def abort(route):
        attempts.append(route.request.url)
        route.abort("connectionreset")

    page.route("**/js/store.js", abort)
    page.goto(instance.base_url)
    for _ in range(100):
        if len(attempts) > RELOADS:
            break
        page.wait_for_timeout(100)
    page.wait_for_timeout(1000)  # no further reload follows the last one
    page.wait_for_load_state()
    assert len(attempts) == RELOADS + 1
    assert page.locator(".management-bar[data-ready]").count() == 0
