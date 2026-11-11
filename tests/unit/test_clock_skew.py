"""A Device's clock skew from its Date header. A Device is untrusted input:
a header that is missing or unreadable is no reading, never an error."""

from email.utils import formatdate

import pytest

from camera_viewer.device_client import _clock_skew

NOW = 1_800_000_000.0


def test_skew_is_the_device_clock_minus_ours():
    assert _clock_skew([(b"Date", formatdate(NOW - 7200, usegmt=True).encode())], NOW) == -7200
    assert _clock_skew([(b"date", formatdate(NOW + 90, usegmt=True).encode())], NOW) == 90


def test_minus_zero_zone_is_utc():
    assert _clock_skew([(b"Date", formatdate(NOW).encode().replace(b"+0000", b"-0000"))], NOW) == 0


@pytest.mark.parametrize("headers", [[], [(b"Date", b"yesterday")], [(b"Date", b"\xff\xfe")],
                                     [(b"Date", b"Tue, 99 Oct 2026 25:61:00 GMT")]])
def test_no_readable_date_is_no_reading(headers):
    assert _clock_skew(headers, NOW) is None
