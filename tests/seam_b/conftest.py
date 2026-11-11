"""Seam B fixtures and diagnostics.

``offline`` is a browser context that can reach only the shared instance, for
tests that prove the UI works with no internet (``harness.offline``).
``placed`` puts Devices on Map View at Fixed Positions (``harness.map_view``).

When a browser test fails, print what the browser noted going wrong during it
and what the shared instance logged, since the assertion alone rarely says why
a Feed did not play or a page stayed empty."""

import pytest

from harness.map_view import HERE, place
from harness.offline import Offline


@pytest.fixture
def offline(browser, instance) -> Offline:
    """A logged-in browser context that can reach the shared instance and
    nothing else: every other request is aborted and listed in
    ``offline.blocked``. Open pages with ``offline.new_page()``; a test of the
    login page clears ``offline.context``'s cookies first."""
    guard = Offline(browser.new_context(), instance)
    yield guard
    guard.close()


@pytest.fixture
def placed(instance, test_pattern_device, unique_label):
    """Add the shared test-pattern Device under a new label at a Fixed Position:
    ``placed(prefix, where=HERE)`` returns the label. The labels are removed
    again afterwards."""
    labels = []

    def add(prefix: str, where: dict = HERE) -> str:
        label = unique_label(prefix)
        instance.add_device(test_pattern_device, label=label)
        labels.append(label)
        place(instance, label, where)
        return label

    yield add
    with instance.client() as api:
        for label in labels:
            api.delete(f"/api/devices/{label}")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == "call":
        item.seam_b_failed = report.failed
        browser = item.funcargs.get("browser")
        if report.failed and browser is not None:
            browser.diagnostics.extend(browser.page_states())  # the pages close at teardown
    return report


@pytest.fixture(autouse=True)
def browser_diagnostics_on_failure(request):
    if "browser" not in request.fixturenames:
        yield
        return
    browser = request.getfixturevalue("browser")
    browser.diagnostics.clear()
    yield
    if getattr(request.node, "seam_b_failed", False):
        print("\n--- browser diagnostics ---\n" + ("\n".join(browser.diagnostics) or "(nothing noted)"))


@pytest.fixture(autouse=True)
def instance_logs_on_failure(request):
    yield
    if getattr(request.node, "seam_b_failed", False) and "instance" in request.fixturenames:
        instance = request.getfixturevalue("instance")
        print(f"\n--- instance logs (tail) ---\n{instance.logs()[-8000:]}")
