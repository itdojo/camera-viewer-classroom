"""One error shape for the whole API: ``{"error": <code>, "message": <text>, ...extra}``.

Raise ``ApiError`` from any router. ``code`` is stable and machine-readable
(the frontend and the tests branch on it); ``message`` is for people; extra
keyword arguments become extra fields, such as the presented ``fingerprint``.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from camera_viewer.registry import Device, DeviceNotFound, DeviceRegistry

# The HTTP status for each Device Registry refusal, by its code. Unknown codes are 422.
REGISTRY_STATUS = {
    "label_taken": 409,
    "camera_exists": 409,
    "device_not_found": 404,
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, *, headers: dict[str, str] | None = None,
                 **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers
        self.extra = extra


def registry_error(exc: Exception) -> ApiError:
    """The API's answer to a ``camera_viewer.registry.RegistryError``."""
    code = getattr(exc, "code", "registry_error")
    return ApiError(REGISTRY_STATUS.get(code, 422), code, str(exc))


def registered_device(registry: DeviceRegistry, label: str) -> Device:
    """The registered Device ``label``, or the API's 404 ``device_not_found``."""
    device = registry.get(label)
    if device is None:
        raise registry_error(DeviceNotFound(label))
    return device


async def _handle(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": exc.code, "message": exc.message, **exc.extra}, status_code=exc.status,
                        headers=exc.headers)


def install(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _handle)
