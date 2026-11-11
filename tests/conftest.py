import pytest

from harness.fixtures import (  # noqa: F401  (pytest fixtures)
    browser,
    device_harness,
    instance,
    instance_image,
    instances,
    test_pattern_device,
    unique_label,
)


def pytest_collection_modifyitems(items):
    """Mark every test that needs a Docker daemon ``docker``, so ``-m "not docker"`` is the fast tier.

    Every harness fixture (instance, instances, test_pattern_device, browser, and pytest-playwright's
    page and context through browser) depends on ``device_harness``, so a test whose fixture closure
    holds it needs Docker. A test that drives Docker itself, without the harness, marks its module
    with ``pytestmark = pytest.mark.docker``. Rendering compose files (``docker compose config``)
    needs only the CLI, so those tests stay in the fast tier.
    """
    for item in items:
        if "device_harness" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.docker)


@pytest.fixture
def page(page):
    """pytest-playwright's page, left the way a student leaves a tab: navigating away fires
    pagehide, so the UI ends its WHEP sessions. A page closed outright leaves them on the
    Relay for about 30 s, still counted as viewers by the Feed Monitor."""
    yield page
    if not page.is_closed():
        page.goto("about:blank")


@pytest.fixture(autouse=True)
def no_leaked_devices(request):
    """Remove any Device a test started and left running. Each test-pattern Device encodes 720p30
    in software, so leaked ones pile up for the rest of the session; on a 4-vCPU CI runner they
    starved the Devices still in use below real time. Devices from module- and session-scoped
    fixtures exist before this snapshot and are left alone. Only tests that already use the
    harness are touched, so a Docker-free test stays Docker-free."""
    if "device_harness" not in request.fixturenames:
        yield
        return
    harness = request.getfixturevalue("device_harness")
    before = set(harness.devices) | set(harness.scripted)
    yield
    for label in set(harness.devices) - before:
        harness.remove_device(harness.devices[label])
    for label in set(harness.scripted) - before:
        harness.remove_scripted_device(harness.scripted[label])
