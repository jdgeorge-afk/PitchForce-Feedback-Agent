"""FastAPI application: server-rendered pages for the event operator plus a small JSON API
(chunked uploads, job progress) used by the browser."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from ..auth import LoginRequired
from ..config import get_settings
from .common import templates

log = logging.getLogger("pitchforce.web")


def _err_ctx() -> dict:
    from .common import DEFAULT_THEME

    return {"user": None, "theme": DEFAULT_THEME, "csrf": "", "flashes": [], "event": None, "settings": get_settings()}


def create_app() -> FastAPI:
    s = get_settings()
    if s.environment == "production" and s.secret_key.startswith("dev-insecure"):
        raise RuntimeError("Set SECRET_KEY before running in production.")
    app = FastAPI(title="PitchForce Feedback", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=s.secret_key, session_cookie="pf_session",
                       max_age=s.session_max_age_hours * 3600, same_site="lax", https_only=s.cookie_secure)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        if request.url.path.startswith(("/events", "/artifacts", "/settings", "/api")):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    from . import api, views_auth, views_company, views_events, views_review, views_settings

    for mod in (views_auth, views_events, views_company, views_review, views_settings, api):
        app.include_router(mod.router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/api/"):
            from fastapi.responses import JSONResponse

            return JSONResponse({"error": "Not signed in"}, status_code=401)
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/"):
            from fastapi.responses import JSONResponse

            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(request, "error.html", {**_err_ctx(), "status": exc.status_code, "message": exc.detail},
                                          status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("Unhandled error on %s", request.url.path)
        return HTMLResponse(templates.get_template("error.html").render(
            request=request, status=500, **_err_ctx(),
            message="Something went wrong. The error was logged; please try again or contact your administrator."), status_code=500)

    return app


app = create_app()
