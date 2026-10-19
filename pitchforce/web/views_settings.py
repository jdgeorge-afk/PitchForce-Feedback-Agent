"""Settings: report wording, best practices, services/providers, calendar sources, integrations, theme, users."""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import record
from ..auth import check_csrf, create_user, current_user
from ..config import get_settings
from ..db import get_db
from ..jobs.queue import enqueue
from ..media.transcription import describe as transcription_describe
from ..models import AppSetting, BestPracticeVersion, CalendarSession, Event, EventAccess, User
from ..services import bestpractices, calendar
from ..services.interest import category_rules
from ..util import clean_text
from .common import flash, page

router = APIRouter()
SECTIONS = ["wording", "best-practices", "services", "calendar", "integrations", "users"]


def _require_admin(user: User) -> None:
    if not user.is_admin:
        raise HTTPException(403, "Only administrators can change this setting.")


@router.get("/settings")
def settings_page(request: Request, section: str = "wording", db: Session = Depends(get_db), user: User = Depends(current_user)):
    section = section if section in SECTIONS else "wording"
    s = get_settings()
    bp = bestpractices.current(db)
    ctx = {"user": user, "section": section, "sections": SECTIONS, "bp": bp,
           "bp_versions": list(db.scalars(select(BestPracticeVersion).order_by(BestPracticeVersion.version.desc()))),
           "wording": (db.get(AppSetting, "report_wording").value if db.get(AppSetting, "report_wording") else {}),
           "rules": category_rules(db), "s": s}
    if section == "services":
        from ..analysis.providers import get_provider

        ctx["ai"] = get_provider().describe()
        ctx["ai_configured"] = s.ai_configured
        ctx["transcription"] = transcription_describe()
    elif section == "calendar":
        ctx["cal"] = calendar.cache_status(db)
        ctx["sessions"] = list(db.scalars(select(CalendarSession).order_by(CalendarSession.start_at_utc.desc().nullslast()).limit(50)))
        from ..db import utcnow

        ctx["now"] = utcnow()
    elif section == "users":
        ctx["users"] = list(db.scalars(select(User).order_by(User.email)))
        ctx["events"] = list(db.scalars(select(Event).where(Event.archived_at.is_(None)).order_by(Event.event_date.desc())))
    imp = db.get(AppSetting, "bp_import")
    ctx["bp_import"] = imp.value if imp else None
    return page(request, db, "settings.html", ctx)


@router.post("/settings/wording", dependencies=[Depends(check_csrf)])
async def save_wording(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _require_admin(user)
    f = await request.form()
    st = db.get(AppSetting, "report_wording") or AppSetting(key="report_wording", value={})
    st.value = {**(st.value or {}), "advisory_enabled": f.get("advisory_enabled") == "on"}
    db.merge(st)
    theme = db.get(AppSetting, "theme") or AppSetting(key="theme", value={})
    vals = {}
    for k in ("primary", "accent"):
        v = (f.get(k) or "").strip()
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
            vals[k] = v
    if f.get("brand"):
        vals["brand"] = clean_text(f.get("brand"), 80)
    theme.value = {**(theme.value or {}), **vals}
    db.merge(theme)
    rules_txt = (f.get("rules") or "").strip()
    if rules_txt:
        rules = []
        for line in rules_txt.splitlines():
            if "=>" in line:
                pat, cat = [x.strip() for x in line.split("=>", 1)]
                try:
                    re.compile(pat)
                except re.error as e:
                    flash(request, f"Invalid pattern {pat!r}: {e}", "error")
                    return RedirectResponse("/settings?section=wording", status_code=303)
                rules.append({"pattern": pat, "category": cat})
        db.merge(AppSetting(key="interest_rules", value={"rules": rules}))
    record(db, "settings:wording", "settings", "", None, after={"theme": vals}, user_id=user.id)
    db.commit()
    flash(request, "Settings saved.", "success")
    return RedirectResponse("/settings?section=wording", status_code=303)


@router.post("/settings/best-practices", dependencies=[Depends(check_csrf)])
async def save_bp(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _require_admin(user)
    f = await request.form()
    cur = bestpractices.current(db)
    content = json.loads(json.dumps(cur.content))
    principles = []
    for line in (f.get("principles") or "").splitlines():
        line = line.strip().lstrip("-• ").strip()
        if line:
            old = next((p for p in content["principles"] if p["text"] == line), None)
            principles.append(old or {"id": f"p{len(principles)}", "text": clean_text(line, 500), "themes": []})
    if principles:
        content["principles"] = principles
    for k in ("virtual", "in_person", "hybrid"):
        if f.get(f"format_{k}") is not None:
            content["format_guidance"][k] = clean_text(f.get(f"format_{k}"), 500)
    for k in ("best_practices_intro", "interest_reference", "no_guarantee"):
        if f.get(k) is not None:
            content["report_wording"][k] = clean_text(f.get(k), 500)
    content["cadence_guidance"]["text"] = clean_text(f.get("cadence") or content["cadence_guidance"]["text"], 800)
    issues = cur.issues
    for i in issues:
        if f.get(f"issue_{i['id']}") == "acknowledged":
            i["status"] = "acknowledged"
    v = bestpractices.new_version(db, content, clean_text(f.get("notes") or "Edited by operator", 500), user.id, approve=True, issues=issues)
    record(db, "best_practices:new_version", "best_practices", v.id, None, after={"version": v.version}, user_id=user.id)
    db.commit()
    flash(request, f"Best-practices version {v.version} approved. Regenerate reports to use it.", "success")
    return RedirectResponse("/settings?section=best-practices", status_code=303)


@router.post("/settings/best-practices/upload", dependencies=[Depends(check_csrf)])
async def upload_bp(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    _require_admin(user)
    name = file.filename or "upload"
    if not name.lower().endswith((".docx", ".pdf", ".zip", ".txt", ".md")):
        flash(request, "Upload a DOCX, PDF, TXT or ZIP file.", "error")
        return RedirectResponse("/settings?section=best-practices", status_code=303)
    data = await file.read(get_settings().max_document_upload_mb * 1024 * 1024 + 1)
    if len(data) > get_settings().max_document_upload_mb * 1024 * 1024:
        flash(request, "File is too large.", "error")
        return RedirectResponse("/settings?section=best-practices", status_code=303)
    res = bestpractices.import_documents(name, data)
    if res.get("error"):
        flash(request, res["error"], "error")
    else:
        cur = bestpractices.current(db)
        issues = list(cur.issues)
        for it in res["issues"]:
            iid = "doc-" + re.sub(r"\W+", "-", (it["doc"] + it["issue"]).lower())[:60]
            if not any(x["id"] == iid for x in issues):
                issues.append({"id": iid, "severity": "high" if "behalf" in it["issue"] or "guarantee" in it["issue"].lower() else "medium",
                               "text": f"{it['doc']}: {it['issue']} — “{it['excerpt'][:200]}”", "status": "open"})
        cur.issues = issues
        db.merge(AppSetting(key="bp_import", value={k: v for k, v in res.items() if k != "texts"} | {"filename": name,
                                                                                                    "texts": res["texts"]}))
        flash(request, f"Read {len(res['documents'])} document(s): {len(res['preferred'])} current, {len(res['duplicates'])} duplicate(s), "
                       f"{sum(1 for d in res['documents'] if d['archived'])} archived copies (not used as current policy), "
                       f"{len(res['issues'])} wording issue(s) flagged. Review the extracted text, then edit and approve a new version.",
              "success")
    record(db, "best_practices:upload", "best_practices", "", None, after={"file": name}, user_id=user.id)
    db.commit()
    return RedirectResponse("/settings?section=best-practices", status_code=303)


@router.post("/settings/calendar/refresh", dependencies=[Depends(check_csrf)])
def refresh_calendar(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from ..db import utcnow

    enqueue(db, "calendar_refresh", utcnow().strftime("%Y%m%d%H%M%S"), {}, user_id=user.id)
    db.commit()
    flash(request, "Calendar refresh started in the background. Existing listings stay available if it fails.", "info")
    return RedirectResponse("/settings?section=calendar", status_code=303)


@router.post("/settings/users", dependencies=[Depends(check_csrf)])
async def users_action(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _require_admin(user)
    f = await request.form()
    a = f.get("action")
    if a == "create":
        try:
            u = create_user(db, f.get("email", ""), f.get("password", ""), f.get("name", ""), is_admin=f.get("is_admin") == "on")
            flash(request, f"User {u.email} created.", "success")
        except Exception as e:  # noqa: BLE001
            db.rollback()
            flash(request, f"Could not create user: {e}", "error")
    elif a == "grant":
        u = db.get(User, f.get("user_id"))
        ev = db.get(Event, f.get("event_id"))
        if u and ev and not db.scalar(select(EventAccess).where(EventAccess.event_id == ev.id, EventAccess.user_id == u.id)):
            db.add(EventAccess(event_id=ev.id, user_id=u.id, role="editor"))
            flash(request, f"{u.email} can now open “{ev.name}”.", "success")
    elif a == "disable":
        u = db.get(User, f.get("user_id"))
        if u and u.id != user.id:
            u.disabled = not u.disabled
            flash(request, f"{u.email} {'disabled' if u.disabled else 'enabled'}.", "info")
    record(db, f"users:{a}", "user", f.get("user_id") or "", None, after={"email": f.get("email", "")}, user_id=user.id)
    db.commit()
    return RedirectResponse("/settings?section=users", status_code=303)
