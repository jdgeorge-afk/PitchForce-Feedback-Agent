"""Sign-in, sign-out, and first-run administrator setup."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import record
from ..auth import authenticate, check_csrf, create_user, current_user_optional
from ..db import get_db
from ..models import User
from .common import flash, page

router = APIRouter()
_attempts: dict[str, list[float]] = {}


def _rate_limited(key: str, limit: int = 8, window: int = 300) -> bool:
    now = time.time()
    hits = [t for t in _attempts.get(key, []) if now - t < window]
    _attempts[key] = hits
    return len(hits) >= limit


@router.get("/login")
def login_form(request: Request, next: str = "/", db: Session = Depends(get_db), user=Depends(current_user_optional)):
    if db.scalar(select(User.id).limit(1)) is None:
        return RedirectResponse("/setup", status_code=303)
    if user:
        return RedirectResponse("/", status_code=303)
    return page(request, db, "login.html", {"next": next if next.startswith("/") and not next.startswith("//") else "/"})


@router.post("/login", dependencies=[Depends(check_csrf)])
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/"), db: Session = Depends(get_db)):
    key = (request.client.host if request.client else "?") + ":" + email.lower()
    if _rate_limited(key):
        flash(request, "Too many sign-in attempts. Wait a few minutes and try again.", "error")
        return RedirectResponse("/login", status_code=303)
    u = authenticate(db, email, password)
    if not u:
        _attempts.setdefault(key, []).append(time.time())
        flash(request, "Email or password is incorrect.", "error")
        return RedirectResponse("/login", status_code=303)
    request.session.clear()
    request.session["uid"] = u.id
    record(db, "login", "user", u.id, user_id=u.id)
    db.commit()
    return RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", status_code=303)


@router.post("/logout", dependencies=[Depends(check_csrf)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/setup")
def setup_form(request: Request, db: Session = Depends(get_db)):
    if db.scalar(select(User.id).limit(1)) is not None:
        return RedirectResponse("/login", status_code=303)
    return page(request, db, "setup.html", {})


@router.post("/setup", dependencies=[Depends(check_csrf)])
def setup(request: Request, email: str = Form(...), name: str = Form(""), password: str = Form(...), password2: str = Form(...),
          db: Session = Depends(get_db)):
    if db.scalar(select(User.id).limit(1)) is not None:
        return RedirectResponse("/login", status_code=303)
    if password != password2:
        flash(request, "The passwords do not match.", "error")
        return RedirectResponse("/setup", status_code=303)
    try:
        u = create_user(db, email, password, name, is_admin=True)
    except ValueError as e:
        flash(request, str(e), "error")
        return RedirectResponse("/setup", status_code=303)
    request.session.clear()
    request.session["uid"] = u.id
    flash(request, "Administrator account created. Welcome!", "success")
    return RedirectResponse("/", status_code=303)
