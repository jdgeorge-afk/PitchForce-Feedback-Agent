"""Authentication and authorization.

- Passwords are hashed with scrypt (stdlib) and a per-user salt.
- Sessions use a signed, HTTP-only cookie (Starlette SessionMiddleware).
- Every event-scoped route calls ``require_event`` which enforces access server-side.
- State-changing form posts carry a CSRF token bound to the session.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import Company, Event, EventAccess, User

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def create_user(db: Session, email: str, password: str, name: str = "", is_admin: bool = False) -> User:
    if len(password) < 10:
        raise ValueError("Password must be at least 10 characters")
    u = User(email=email.strip().lower(), name=name or email, password_hash=hash_password(password), is_admin=is_admin)
    db.add(u)
    db.commit()
    return u


def authenticate(db: Session, email: str, password: str) -> User | None:
    u = db.scalar(select(User).where(User.email == email.strip().lower()))
    if u and not u.disabled and verify_password(password, u.password_hash):
        return u
    # Equalize timing a little for unknown users.
    if not u:
        verify_password(password, hash_password("timing-equalizer"))
    return None


class LoginRequired(Exception):
    pass


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    u = db.get(User, uid)
    if not u or u.disabled:
        request.session.clear()
        return None
    return u


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    u = current_user_optional(request, db)
    if not u:
        raise LoginRequired()
    return u


def can_access_event(db: Session, user: User, event_id: str) -> bool:
    if user.is_admin:
        return True
    ev = db.get(Event, event_id)
    if not ev:
        return False
    if ev.created_by == user.id:
        return True
    return db.scalar(select(EventAccess).where(EventAccess.event_id == event_id, EventAccess.user_id == user.id)) is not None


def require_event(db: Session, user: User, event_id: str) -> Event:
    ev = db.get(Event, event_id)
    if not ev or not can_access_event(db, user, event_id):
        # Same response for missing and forbidden, so IDs cannot be probed.
        raise HTTPException(status_code=404, detail="Event not found")
    return ev


def require_company(db: Session, user: User, event_id: str, company_id: str) -> tuple[Event, Company]:
    ev = require_event(db, user, event_id)
    c = db.get(Company, company_id)
    if not c or c.event_id != ev.id:
        raise HTTPException(status_code=404, detail="Company not found")
    return ev, c


# -- CSRF -----------------------------------------------------------------
def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


async def check_csrf(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    expected = request.session.get("csrf")
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        ctype = request.headers.get("content-type", "")
        if "form" in ctype:
            form = await request.form()
            supplied = form.get("csrf_token")
    if not expected or not supplied or not hmac.compare_digest(str(expected), str(supplied)):
        raise HTTPException(status_code=403, detail="Your session form token expired. Reload the page and try again.")
