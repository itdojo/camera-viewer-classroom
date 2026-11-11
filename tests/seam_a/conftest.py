"""Seam A diagnostics: when an API test fails, print what the shared
instance logged during it, since a response alone rarely says why the Relay
did what it did."""

import pytest


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == "call":
        item.seam_a_failed = report.failed
    return report


@pytest.fixture(autouse=True)
def instance_logs_on_failure(request):
    yield
    if getattr(request.node, "seam_a_failed", False) and "instance" in request.fixturenames:
        instance = request.getfixturevalue("instance")
        print(f"\n--- instance logs (tail) ---\n{instance.logs()[-8000:]}")


@pytest.fixture
def own_device(instance, device_harness, unique_label):
    """Make a Device of this test's own (so it can be stopped, paused or rebuilt) and add it to
    the shared instance; it is removed from both afterwards, so the shared Camera list stays short."""
    made = []

    def make(prefix):
        device = device_harness.add_device(unique_label(prefix))
        instance.add_device(device)
        made.append(device)
        return device

    yield make
    for device in made:
        with instance.client() as api:
            api.delete(f"/api/devices/{device.label}")
        device_harness.remove_device(device)
