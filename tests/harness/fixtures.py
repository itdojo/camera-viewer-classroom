"""pytest fixtures over the Device and instance harnesses. Imported by tests/conftest.py."""

from __future__ import annotations

import secrets

import pytest

from harness.browser import LoggedInBrowser
from harness.devices import Device, DeviceHarness
from harness.instance import Instance, InstancePool, build_image, remove_image


@pytest.fixture(scope="session")
def device_harness(tmp_path_factory) -> DeviceHarness:
    """The session's Device harness: its own network, torn down at the end.

    Bring up extra Devices with ``device_harness.add_device("<label>")``;
    they are removed with the session unless a test removes them first.
    """
    with DeviceHarness(tmp_path_factory.mktemp("devices")) as harness:
        yield harness


@pytest.fixture(scope="session")
def test_pattern_device(device_harness: DeviceHarness) -> Device:
    """One shared test-pattern Device, publishing and ready.

    Shared across the session: tests that stop, pause or re-certify a Device
    should add their own with ``device_harness.add_device`` instead.
    """
    return device_harness.add_device("tp-shared")


@pytest.fixture(scope="session")
def instance_image(device_harness: DeviceHarness) -> str:
    """The instance image, built once per session from the working tree."""
    tag = build_image(device_harness.session_id)
    yield tag
    remove_image(tag)


@pytest.fixture(scope="session")
def instances(device_harness: DeviceHarness, instance_image: str, tmp_path_factory) -> InstancePool:
    """Start extra instances with ``instances.start("<name>", env={...})``.

    Use your own instance when a test restarts it, tunes it through the
    environment, or needs it empty. Every instance is removed at session end.
    """
    pool = InstancePool(device_harness, instance_image, tmp_path_factory.mktemp("instances"))
    yield pool
    pool.close()


@pytest.fixture(scope="session")
def instance(instances: InstancePool) -> Instance:
    """One shared instance, on the session network with the session's Devices.

    Shared across the session: add Devices under a ``unique_label`` and never
    restart or reconfigure it.
    """
    return instances.start("shared")


@pytest.fixture
def unique_label():
    """Make a kebab-case Device label no other test in the session uses."""
    def make(prefix: str = "dev") -> str:
        return f"{prefix}-{secrets.token_hex(3)}"
    return make


@pytest.fixture(scope="session")
def browser(browser, instances: InstancePool) -> LoggedInBrowser:
    """pytest-playwright's browser, logged in to every instance of the session.

    Every context and page it opens (and so the ``context`` and ``page``
    fixtures) starts past the login wall, including for instances started
    after it opened. A test of the login wall itself calls
    ``page.context.clear_cookies()`` first.
    """
    return LoggedInBrowser(browser, instances)
