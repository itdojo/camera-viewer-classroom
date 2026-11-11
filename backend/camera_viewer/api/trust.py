"""Trust on first use, shared by adding a Device and adding a manual RTSPS Camera.

The first request carries no fingerprint and is answered 409
``fingerprint_unconfirmed`` with the fingerprint the endpoint presents; the
student compares it with the one ``gen-cert.sh`` printed and sends the form
again with it. A fingerprint the endpoint does not present is 409
``fingerprint_mismatch``, again carrying the presented one.
"""

from __future__ import annotations

from camera_viewer.api.errors import ApiError
from camera_viewer.device_client import (
    CredentialsRejected,
    DeviceClient,
    DeviceError,
    FingerprintMismatch,
    PermissionDenied,
    normalize_fingerprint,
)


async def confirm_fingerprint(address: str, confirmed: str | None, *, what: str = "Device") -> str:
    """The fingerprint ``address`` (``host[:port]``) presents, once the student
    has confirmed exactly that one."""
    try:
        presented = await DeviceClient(address).presented_fingerprint()
    except DeviceError as exc:
        raise device_error(exc) from exc
    confirmed = normalize_fingerprint(confirmed or "")
    if not confirmed:
        raise ApiError(409, "fingerprint_unconfirmed",
                       f"confirm the {what}'s certificate fingerprint to add it", fingerprint=presented)
    if confirmed != presented:
        raise ApiError(409, "fingerprint_mismatch",
                       f"the {what} presents a different certificate from the fingerprint you confirmed",
                       fingerprint=presented)
    return confirmed


def device_error(exc: DeviceError) -> ApiError:
    """The API's answer to a Device Client error."""
    if isinstance(exc, FingerprintMismatch):  # the certificate changed between two reads
        return ApiError(409, exc.code, str(exc), fingerprint=exc.presented)
    if isinstance(exc, CredentialsRejected | PermissionDenied):  # the student's to fix on the Device
        return ApiError(422, exc.code, str(exc))
    return ApiError(502, exc.code, str(exc))
