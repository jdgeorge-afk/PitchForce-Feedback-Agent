"""Shared web helpers: templates, filters, flash messages, form CSRF dependency."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import csrf_token
from ..config import get_settings
from ..models import AppSetting, ReviewItem
from ..reports import markup
from ..util import fmt_duration, fmt_hms

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
env = templates.env
env.filters["md"] = lambda s: Markup(markup.to_html(s or ""))
env.filters["hms"] = lambda v: fmt_hms(v)
env.filters["dur"] = lambda v: fmt_duration(v)
env.filters["dt"] = lambda d: d.strftime("%Y-%m-%d %H:%M") if isinstance(d, datetime) else (d or "")
env.filters["num"] = lambda v, d=1: "—" if v is None else (f"{v:.{d}f}".rstrip("0").rstrip(".") if d else f"{v:.0f}")
env.filters["mb"] = lambda b: f"{(b or 0) / 1024 / 1024:.1f} MB"
env.filters["label"] = lambda s: (s or "").replace("_", " ")
env.globals["now"] = datetime.utcnow

DEFAULT_THEME = {"primary": "#1F4E5F", "accent": "#C8702A", "brand": "EcoVG · PitchForce Feedback"}


def theme(db: Session) -> dict:
    t = db.get(AppSetting, "theme")
    return {**DEFAULT_THEME, **(t.value if t else {})}


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session.setdefault("flash", []).append({"m": message, "k": kind})


def page(request: Request, db: Session, name: str, ctx: dict, status_code: int = 200):
    flashes = request.session.pop("flash", [])
    base = {
        "user": ctx.get("user"), "csrf": csrf_token(request), "flashes": flashes, "theme": theme(db),
        "settings": get_settings(),
    }
    ev = ctx.get("event")
    if ev is not None:
        base["open_reviews"] = db.scalar(select(func.count(ReviewItem.id)).where(ReviewItem.event_id == ev.id,
                                                                                 ReviewItem.status == "open")) or 0
    return templates.TemplateResponse(request, name, {**base, **ctx}, status_code=status_code)
