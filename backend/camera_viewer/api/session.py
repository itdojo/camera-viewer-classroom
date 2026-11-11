"""Session: log in with the instance's shared password, log out, ask whether logged in.

    POST   /api/session   {"password": ...}  200 + session cookie, 401 wrong_password, or 429
                          too_many_attempts (with Retry-After) while the client is locked out
    GET    /api/session   {"authenticated": bool}
    DELETE /api/session   204, the session revoked and its cookie cleared

``/api/session`` and the login page (``GET /login``, mounted by ``pages``)
are the only things ``camera_viewer.auth.SessionGate`` lets through without a
session.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from camera_viewer.api.errors import ApiError
from camera_viewer.services import Services, services

router = APIRouter(prefix="/session", tags=["session"])
pages = APIRouter(include_in_schema=False)


class Login(BaseModel):
    password: str


@router.post("")
def log_in(form: Login, request: Request, response: Response, svc: Services = Depends(services)) -> dict:
    auth = svc.auth
    client = request.client.host if request.client else "unknown"
    if (wait := auth.throttle.retry_after(client)) > 0:
        seconds = math.ceil(wait)
        raise ApiError(429, "too_many_attempts", f"too many wrong passwords; try again in {seconds} s",
                       headers={"Retry-After": str(seconds)}, retry_after=seconds)
    if not auth.password_matches(form.password):
        auth.throttle.failed(client)
        raise ApiError(401, "wrong_password", "wrong password")
    auth.throttle.succeeded(client)
    response.set_cookie(auth.cookie_name, auth.issue(), max_age=auth.max_age_seconds, path="/",
                        httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return {"authenticated": True}


@router.get("")
def session_state(request: Request, svc: Services = Depends(services)) -> dict:
    return {"authenticated": svc.auth.valid(svc.auth.token_of(request))}


@router.delete("", status_code=204)
def log_out(request: Request, svc: Services = Depends(services)) -> Response:
    svc.auth.revoke(svc.auth.token_of(request))
    response = Response(status_code=204)
    response.delete_cookie(svc.auth.cookie_name, path="/", httponly=True, samesite="strict",
                           secure=request.url.scheme == "https")
    return response


@pages.get("/login")
def login_page(request: Request, svc: Services = Depends(services)) -> Response:
    if svc.auth.valid(svc.auth.token_of(request)):
        return RedirectResponse("/", status_code=303)
    return FileResponse(svc.settings.frontend_dir / "login.html", headers={"Cache-Control": "no-store"})
