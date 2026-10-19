"""Events dashboard, event setup, sources, people & matching, processing, results, downloads."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..audit import record
from ..auth import check_csrf, current_user, require_event
from ..config import get_settings
from ..db import get_db, utcnow
from ..jobs.queue import enqueue
from ..models import (
    RELATION_TYPES,
    AIUsage,
    Artifact,
    Clip,
    Company,
    CompanyAlias,
    EntityRelationship,
    Event,
    EventAccess,
    JudgeAttendance,
    Person,
    PersonAlias,
    ProcessingJob,
    ReviewItem,
    Score,
    SourceFile,
    SpeakerMapping,
    User,
)
from ..services import interest as interest_svc
from ..services import matching
from ..services.inventory import inventory
from ..services.processing import queue_ingest, queue_processing
from ..services.regenerate import stale_artifacts
from ..services.scoring import compute_for_company, current_result, rank_companies
from ..services.sources import KIND_LABELS
from ..storage import get_storage
from ..util import clean_text, norm_email, parse_hms
from .common import flash, page

router = APIRouter()
TIMEZONES = ["America/Los_Angeles", "America/Denver", "America/Chicago", "America/New_York", "America/Phoenix", "Pacific/Honolulu",
             "America/Anchorage", "Europe/London", "UTC"]


def _events_for(db: Session, user: User):
    q = select(Event).where(Event.archived_at.is_(None)).order_by(Event.event_date.desc(), Event.created_at.desc())
    if not user.is_admin:
        q = q.where(or_(Event.created_by == user.id, Event.id.in_(select(EventAccess.event_id).where(EventAccess.user_id == user.id))))
    return list(db.scalars(q))


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = []
    for ev in _events_for(db, user):
        jobs = list(db.scalars(select(ProcessingJob).where(ProcessingJob.event_id == ev.id, ProcessingJob.status.in_(["queued", "running"]))))
        failed = db.scalar(select(func.count(ProcessingJob.id)).where(ProcessingJob.event_id == ev.id, ProcessingJob.status == "failed")) or 0
        rows.append({
            "event": ev, "companies": db.scalar(select(func.count(Company.id)).where(Company.event_id == ev.id)) or 0,
            "reviews": db.scalar(select(func.count(ReviewItem.id)).where(ReviewItem.event_id == ev.id, ReviewItem.status == "open")) or 0,
            "active_jobs": len(jobs), "failed_jobs": failed,
        })
    return page(request, db, "dashboard.html", {"user": user, "rows": rows})


@router.get("/events/new")
def new_event_form(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return page(request, db, "event_form.html", {"user": user, "event": None, "timezones": TIMEZONES, "is_new": True})


def _apply_event_form(ev: Event, form) -> None:
    ev.name = clean_text(form.get("name", ""), 300).strip() or ev.name
    try:
        ev.event_date = date.fromisoformat(form.get("event_date", ""))
    except ValueError as e:
        raise HTTPException(400, "Enter a valid event date.") from e
    tz = form.get("timezone") or "America/Los_Angeles"
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(tz)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Unknown timezone {tz}") from e
    ev.timezone = tz
    ev.format = form.get("format") if form.get("format") in ("virtual", "in_person", "hybrid") else "virtual"
    ev.expected_judge_count = max(1, min(50, int(form.get("expected_judge_count") or 6)))
    ev.allow_fractional_scores = form.get("allow_fractional_scores") == "on"
    pol = form.get("missing_judge_policy", "one_absent_mean")
    ev.missing_judge_policy = pol if pol in ("none", "one_absent_mean", "mean_for_confirmed_missing") else "one_absent_mean"
    ev.score_display_decimals = max(0, min(3, int(form.get("score_display_decimals") or 1)))
    ev.webinar_id = (form.get("webinar_id") or "").strip() or None
    rl = (form.get("replay_link") or "").strip()
    if rl and not rl.startswith("https://"):
        raise HTTPException(400, "The replay link must start with https://")
    ev.replay_link = rl or None
    rs = (form.get("recording_start_wallclock") or "").strip()
    if rs and parse_hms(rs) is None:
        raise HTTPException(400, "Recording start must look like 15:00:14")
    ev.recording_start_wallclock = rs or None
    ev.include_score_in_clip = form.get("include_score_in_clip") == "on"


@router.post("/events/new", dependencies=[Depends(check_csrf)])
async def create_event(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    ev = Event(name="Untitled event", event_date=date.today(), created_by=user.id)
    _apply_event_form(ev, form)
    db.add(ev)
    db.flush()
    db.add(EventAccess(event_id=ev.id, user_id=user.id, role="editor"))
    record(db, "create", "event", ev.id, ev.id, after={"name": ev.name}, user_id=user.id)
    db.commit()
    flash(request, "Event created. Next, upload the event materials.", "success")
    return RedirectResponse(f"/events/{ev.id}/sources", status_code=303)


@router.post("/events/sample", dependencies=[Depends(check_csrf)])
def create_sample(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from ..services.sample import create_sample_event

    ev = create_sample_event(db, user.id)
    db.add(EventAccess(event_id=ev.id, user_id=user.id, role="editor"))
    db.commit()
    flash(request, "Sample event created from synthetic data. Processing runs in the background.", "success")
    return RedirectResponse(f"/events/{ev.id}", status_code=303)


@router.get("/events/{event_id}")
def event_overview(event_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    inv = inventory(db, ev)
    companies = list(db.scalars(select(Company).where(Company.event_id == ev.id).order_by(Company.slot_order)))
    reviews = db.scalar(select(func.count(ReviewItem.id)).where(ReviewItem.event_id == ev.id, ReviewItem.status == "open")) or 0
    high = db.scalar(select(func.count(ReviewItem.id)).where(ReviewItem.event_id == ev.id, ReviewItem.status == "open",
                                                             ReviewItem.severity == "high")) or 0
    arts = db.scalar(select(func.count(Artifact.id)).where(Artifact.event_id == ev.id, Artifact.is_current.is_(True))) or 0
    steps = [
        ("Create event", True, f"/events/{ev.id}/settings"),
        ("Upload materials", any(r["status"] == "present" for r in inv["rows"]), f"/events/{ev.id}/sources"),
        ("Check companies & people", bool(companies), f"/events/{ev.id}/people"),
        ("Run processing", ev.status in ("processing", "review", "complete"), f"/events/{ev.id}#process"),
        ("Resolve review items", ev.status in ("review", "complete") and reviews == 0, f"/events/{ev.id}/review"),
        ("Review reports & clips", any(c.status == "approved" for c in companies), f"/events/{ev.id}/results"),
        ("Download packages", arts > 0, f"/events/{ev.id}/downloads"),
    ]
    cost = db.scalar(select(func.sum(AIUsage.est_cost_usd)).where(AIUsage.event_id == ev.id)) or 0.0
    return page(request, db, "event_overview.html", {"user": user, "event": ev, "inv": inv, "companies": companies, "steps": steps,
                                                     "reviews": reviews, "high": high, "cost": cost})


@router.get("/events/{event_id}/settings")
def event_settings(event_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    return page(request, db, "event_form.html", {"user": user, "event": ev, "timezones": TIMEZONES, "is_new": False})


@router.post("/events/{event_id}/settings", dependencies=[Depends(check_csrf)])
async def save_event_settings(event_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    before = {"expected": ev.expected_judge_count, "policy": ev.missing_judge_policy, "rec_start": ev.recording_start_wallclock}
    form = await request.form()
    _apply_event_form(ev, form)
    record(db, "update", "event", ev.id, ev.id, before=before, after={"expected": ev.expected_judge_count,
                                                                      "policy": ev.missing_judge_policy}, user_id=user.id)
    for c in ev.companies:
        compute_for_company(db, ev, c)
    db.commit()
    flash(request, "Event settings saved. Scores were recalculated; re-run processing to refresh reports and clips.", "success")
    return RedirectResponse(f"/events/{ev.id}", status_code=303)


@router.post("/events/{event_id}/archive", dependencies=[Depends(check_csrf)])
def archive_event(event_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    ev.archived_at = utcnow()
    record(db, "archive", "event", ev.id, ev.id, user_id=user.id)
    db.commit()
    flash(request, f"“{ev.name}” archived.", "info")
    return RedirectResponse("/", status_code=303)


@router.post("/events/{event_id}/delete", dependencies=[Depends(check_csrf)])
def delete_event(event_id: str, request: Request, confirm: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Retention: permanently delete an event, its stored files, and all derived data."""
    ev = require_event(db, user, event_id)
    if confirm.strip() != ev.name:
        flash(request, "Type the event name exactly to confirm deletion.", "error")
        return RedirectResponse(f"/events/{ev.id}/settings", status_code=303)
    name = ev.name
    from ..services.retention import delete_event_files, purge_event_rows

    delete_event_files(db, ev)
    purge_event_rows(db, ev)
    record(db, "delete", "event", event_id, None, before={"name": name}, user_id=user.id)
    db.commit()
    flash(request, f"“{name}” and its files were permanently deleted.", "info")
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
@router.get("/events/{event_id}/sources")
def sources_page(event_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    inv = inventory(db, ev)
    from ..media.transcription import describe

    jobs = {j.payload.get("source_id"): j for j in db.scalars(select(ProcessingJob).where(ProcessingJob.event_id == ev.id,
                                                                                          ProcessingJob.kind == "ingest_source"))}
    return page(request, db, "sources.html", {"user": user, "event": ev, "inv": inv, "kinds": KIND_LABELS, "jobs": jobs,
                                              "transcription": describe(), "s": get_settings()})


@router.post("/events/{event_id}/sources/{source_id}/options", dependencies=[Depends(check_csrf)])
async def source_options(event_id: str, source_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    src = db.get(SourceFile, source_id)
    if not src or src.event_id != ev.id:
        raise HTTPException(404, "Source not found")
    form = await request.form()
    action = form.get("action")
    if action == "time_basis" and src.kind == "transcript":
        tb = form.get("time_basis")
        if tb in ("wall_clock", "media"):
            src.time_basis = tb
            src.meta = {**(src.meta or {}), "time_basis_override": True}
            flash(request, "Timestamp type updated. Re-run processing to update clips.", "success")
    elif action == "primary" and src.kind == "transcript":
        for t in db.scalars(select(SourceFile).where(SourceFile.event_id == ev.id, SourceFile.kind == "transcript")):
            t.meta = {**(t.meta or {}), "primary": t.id == src.id}
        flash(request, f"“{src.original_filename}” will be used for analysis.", "success")
    elif action == "ignore":
        src.status = "ignored"
        flash(request, f"“{src.original_filename}” will not be used.", "info")
    elif action == "reparse":
        src.status = "uploaded"
        queue_ingest(db, ev, src, user.id)
        flash(request, f"Re-reading “{src.original_filename}”.", "info")
    elif action == "transcribe" and src.kind == "video":
        if not get_settings().transcription_configured:
            flash(request, "No transcription provider is configured (see Settings → Services).", "error")
        else:
            enqueue(db, "transcribe", src.id, {"source_id": src.id}, event_id=ev.id, user_id=user.id, rerun=True)
            flash(request, "Transcription started. The audio is sent to the configured provider.", "info")
    record(db, f"source:{action}", "source_file", src.id, ev.id, user_id=user.id)
    db.commit()
    return RedirectResponse(f"/events/{ev.id}/sources", status_code=303)


# ---------------------------------------------------------------------------
# People, companies, matching
# ---------------------------------------------------------------------------
@router.get("/events/{event_id}/people")
def people_page(event_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    companies = list(db.scalars(select(Company).where(Company.event_id == ev.id).order_by(Company.slot_order)))
    people = list(db.scalars(select(Person).where(Person.event_id == ev.id).order_by(Person.role, Person.display_name)))
    att = {a.person_id: a for a in db.scalars(select(JudgeAttendance).where(JudgeAttendance.event_id == ev.id))}
    speakers = list(db.scalars(select(SpeakerMapping).where(SpeakerMapping.event_id == ev.id).order_by(SpeakerMapping.speaker_label)))
    rels = list(db.scalars(select(EntityRelationship).where(EntityRelationship.event_id == ev.id)))
    return page(request, db, "people.html", {"user": user, "event": ev, "companies": companies, "people": people, "att": att,
                                             "speakers": speakers, "rels": rels, "relation_types": RELATION_TYPES,
                                             "people_by_id": {p.id: p for p in people}, "companies_by_id": {c.id: c for c in companies}})


@router.post("/events/{event_id}/people/action", dependencies=[Depends(check_csrf)])
async def people_action(event_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    f = await request.form()
    a = f.get("action")
    scope = "global" if f.get("scope") == "global" else "event"

    def company(cid):
        c = db.get(Company, cid)
        if not c or c.event_id != ev.id:
            raise HTTPException(404, "Company not found")
        return c

    def person(pid):
        p = db.get(Person, pid)
        if not p or p.event_id != ev.id:
            raise HTTPException(404, "Person not found")
        return p

    if a == "add_company":
        name = clean_text(f.get("name", ""), 300).strip()
        if name:
            n = (db.scalar(select(func.max(Company.slot_order)).where(Company.event_id == ev.id)) or 0) + 1
            c = Company(event_id=ev.id, display_name=name, name_raw=name, slot_order=n, provenance={"source": "operator", "user": user.id})
            db.add(c)
            db.flush()
            pname = clean_text(f.get("presenter", ""), 300).strip()
            if pname:
                db.add(Person(event_id=ev.id, display_name=pname, name_raw=pname, email=norm_email(f.get("email")) or None,
                              email_raw=f.get("email") or None, role="presenter", company_id=c.id, source_ref={"source": "operator"}))
            flash(request, f"Added {name}.", "success")
    elif a == "rename_company":
        c = company(f.get("company_id"))
        old = c.display_name
        new = clean_text(f.get("name", ""), 300).strip()
        if new and new != old:
            c.display_name = new
            matching.add_company_alias(db, c, old, "operator", status="confirmed", scope="event", note="Previous display name", user_id=user.id)
            flash(request, f"Renamed “{old}” to “{new}” (the old name is kept as a confirmed alias).", "success")
    elif a == "company_alias":
        c = company(f.get("company_id"))
        alias = clean_text(f.get("alias", ""), 300).strip()
        status = f.get("status") if f.get("status") in ("confirmed", "rejected", "proposed") else "confirmed"
        if alias:
            al = matching.add_company_alias(db, c, alias, "operator", status=status, scope=scope, note=f.get("note", ""), user_id=user.id)
            al.status, al.scope = status, scope
            flash(request, f"Alias “{alias}” {status} for {c.display_name} ({scope} scope).", "success")
    elif a == "alias_status":
        al = db.get(CompanyAlias, f.get("alias_id")) or db.get(PersonAlias, f.get("alias_id"))
        if al is None:
            raise HTTPException(404, "Alias not found")
        owner = db.get(Company, getattr(al, "company_id", None)) if hasattr(al, "company_id") else db.get(Person, al.person_id)
        if owner is None or owner.event_id != ev.id:
            raise HTTPException(404, "Alias not found")
        al.status = f.get("status") if f.get("status") in ("confirmed", "rejected") else al.status
        al.scope = scope
        al.created_by = user.id
        flash(request, f"Alias “{al.alias}” {al.status}.", "success")
    elif a == "person_edit":
        p = person(f.get("person_id"))
        before = {"name": p.display_name, "email": p.email, "affiliation": p.affiliation}
        new = clean_text(f.get("name", ""), 300).strip()
        if new and new != p.display_name:
            matching.add_person_alias(db, p, p.display_name, "operator", status="confirmed", note="Previous display name", user_id=user.id)
            p.display_name = new
        if "email" in f:
            p.email = norm_email(f.get("email")) or None
            p.email_raw = f.get("email") or p.email_raw
        if "affiliation" in f:
            p.affiliation = clean_text(f.get("affiliation"), 300) or None
        if "phone" in f:
            p.phone = clean_text(f.get("phone"), 80) or None
        record(db, "edit", "person", p.id, ev.id, before=before, after={"name": p.display_name}, user_id=user.id)
        flash(request, f"Saved {p.display_name}.", "success")
    elif a == "person_alias":
        p = person(f.get("person_id"))
        alias = clean_text(f.get("alias", ""), 300).strip()
        if alias:
            status = f.get("status") if f.get("status") in ("confirmed", "rejected") else "confirmed"
            al = matching.add_person_alias(db, p, alias, "operator", status=status, scope=scope, note=f.get("note", ""), user_id=user.id)
            al.status, al.scope = status, scope
            flash(request, f"“{alias}” recorded as {status} alias of {p.display_name} ({scope}).", "success")
    elif a == "add_judge":
        name = clean_text(f.get("name", ""), 300).strip()
        if name:
            p = Person(event_id=ev.id, display_name=name, name_raw=name, role="judge", affiliation=f.get("affiliation") or None,
                       email=norm_email(f.get("email")) or None, email_raw=f.get("email") or None, source_ref={"source": "operator"})
            db.add(p)
            db.flush()
            db.add(JudgeAttendance(event_id=ev.id, person_id=p.id, status="unknown", source="operator"))
            flash(request, f"Added judge {name}.", "success")
    elif a == "attendance":
        p = person(f.get("person_id"))
        status = f.get("status")
        if status not in ("present", "absent_confirmed", "abstained_confirmed", "unknown"):
            raise HTTPException(400, "Invalid attendance status")
        att = db.scalar(select(JudgeAttendance).where(JudgeAttendance.event_id == ev.id, JudgeAttendance.person_id == p.id))
        att = att or JudgeAttendance(event_id=ev.id, person_id=p.id)
        att.status, att.source, att.confirmed_by, att.note = status, "operator", user.id, f.get("note", "")
        db.add(att)
        db.flush()
        for c in ev.companies:
            compute_for_company(db, ev, c)
        flash(request, f"{p.display_name}: attendance set to {status.replace('_', ' ')}. Scores recalculated.", "success")
    elif a == "speaker":
        m = db.get(SpeakerMapping, f.get("mapping_id"))
        if not m or m.event_id != ev.id:
            raise HTTPException(404, "Speaker not found")
        pid = f.get("person_id") or None
        if pid:
            p = person(pid)
            m.person_id, m.role, m.status = p.id, p.role, "confirmed"
            if m.speaker_label != p.display_name:
                matching.add_person_alias(db, p, m.speaker_label, "operator", status="confirmed", note="Transcript speaker label",
                                          user_id=user.id)
        else:
            role = f.get("role") if f.get("role") in ("host", "judge", "presenter", "audience", "unknown") else "unknown"
            m.person_id, m.role, m.status = None, role, "confirmed"
        flash(request, f"Speaker “{m.speaker_label}” updated. Re-run processing to apply.", "success")
    elif a == "relationship":
        r = db.get(EntityRelationship, f.get("relationship_id"))
        if not r or r.event_id != ev.id:
            raise HTTPException(404, "Relationship not found")
        rel = f.get("relation")
        from .views_review import apply_relationship

        apply_relationship(db, ev, r, rel, user, scope)
        flash(request, f"Recorded: {r.subject_label} → {rel.replace('_', ' ')}.", "success")
    elif a == "add_score":
        c = company(f.get("company_id"))
        p = person(f.get("person_id"))
        from ..parsing.rosters import parse_score_value

        v, status, reason = parse_score_value(f.get("value", ""), ev.score_min, ev.score_max, ev.allow_fractional_scores)
        db.add(Score(event_id=ev.id, company_id=c.id, company_label_raw=c.display_name, judge_person_id=p.id,
                     judge_label_raw=p.display_name, value_raw=f.get("value", ""), value=v, status=status, status_reason=reason,
                     source_kind="manual", entered_by=user.id, source_ref={"ref": "manual entry"}))
        db.flush()
        from ..services.scoring import mark_duplicates

        mark_duplicates(db, ev)
        compute_for_company(db, ev, c)
        flash(request, f"Score {f.get('value')} for {c.display_name} from {p.display_name}: {status.replace('_', ' ')}.",
              "success" if status == "valid" else "error")
    else:
        raise HTTPException(400, "Unknown action")
    record(db, f"people:{a}", "event", ev.id, ev.id, after={k: v for k, v in f.items() if k not in ("csrf_token", "email", "phone")},
           user_id=user.id)
    db.commit()
    back = f.get("back") or f"/events/{ev.id}/people"
    return RedirectResponse(back if back.startswith("/events/") else f"/events/{ev.id}/people", status_code=303)


# ---------------------------------------------------------------------------
# Processing, results, downloads
# ---------------------------------------------------------------------------
@router.post("/events/{event_id}/process", dependencies=[Depends(check_csrf)])
def process(event_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    queue_processing(db, ev, user.id)
    record(db, "process", "event", ev.id, ev.id, user_id=user.id)
    db.commit()
    flash(request, "Processing started. You can leave this page; progress is saved.", "info")
    return RedirectResponse(f"/events/{ev.id}#process", status_code=303)


@router.get("/events/{event_id}/results")
def results(event_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    companies = list(db.scalars(select(Company).where(Company.event_id == ev.id).order_by(Company.slot_order)))
    res = {c.id: current_result(db, c.id) for c in companies}
    ranking = {cid: (rank, tied) for cid, v, rank, tied in rank_companies(
        [(c.id, res[c.id].adjusted_total if res[c.id] and res[c.id].status in ("verified", "verified_with_imputation") else None)
         for c in companies])}
    rows = []
    for c in companies:
        clip = db.scalar(select(Clip).where(Clip.company_id == c.id, Clip.variant == "core"))
        rows.append({"c": c, "r": res[c.id], "rank": ranking.get(c.id), "interest": interest_svc.summarize(db, ev, c), "clip": clip,
                     "reviews": db.scalar(select(func.count(ReviewItem.id)).where(ReviewItem.company_id == c.id, ReviewItem.status == "open")) or 0,
                     "stale": len(stale_artifacts(db, c))})
    return page(request, db, "results.html", {"user": user, "event": ev, "rows": rows})


@router.get("/events/{event_id}/downloads")
def downloads(event_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    companies = list(db.scalars(select(Company).where(Company.event_id == ev.id).order_by(Company.slot_order)))
    arts = {}
    stale = {}
    for c in companies:
        arts[c.id] = list(db.scalars(select(Artifact).where(Artifact.company_id == c.id, Artifact.is_current.is_(True))
                                     .order_by(Artifact.kind)))
        stale[c.id] = {a.id for a in stale_artifacts(db, c)}
    event_arts = list(db.scalars(select(Artifact).where(Artifact.event_id == ev.id, Artifact.company_id.is_(None),
                                                        Artifact.is_current.is_(True))))
    jobs = list(db.scalars(select(ProcessingJob).where(ProcessingJob.event_id == ev.id,
                                                       ProcessingJob.kind.in_(["build_company_package", "build_event_package"]))
                           .order_by(ProcessingJob.created_at.desc()).limit(10)))
    return page(request, db, "downloads.html", {"user": user, "event": ev, "companies": companies, "arts": arts, "stale": stale,
                                                "event_arts": event_arts, "jobs": jobs})


@router.post("/events/{event_id}/packages", dependencies=[Depends(check_csrf)])
async def build_packages(event_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    f = await request.form()
    include_clip = f.get("include_clip") == "on"
    stamp = utcnow().strftime("%Y%m%d%H%M%S")
    if f.get("company_id"):
        c = db.get(Company, f.get("company_id"))
        if not c or c.event_id != ev.id:
            raise HTTPException(404, "Company not found")
        enqueue(db, "build_company_package", f"{c.id}:{stamp}", {"company_id": c.id, "include_clip": include_clip},
                event_id=ev.id, company_id=c.id, user_id=user.id)
        flash(request, f"Building the package for {c.display_name}…", "info")
    else:
        enqueue(db, "build_event_package", f"{ev.id}:{stamp}", {"include_clips": include_clip}, event_id=ev.id, user_id=user.id)
        flash(request, "Building the complete event package…", "info")
    db.commit()
    return RedirectResponse(f"/events/{ev.id}/downloads", status_code=303)


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    a = db.get(Artifact, artifact_id)
    if not a:
        raise HTTPException(404, "File not found")
    require_event(db, user, a.event_id)  # server-side authorization for every artifact
    path = get_storage().path(a.storage_key)
    if not path.exists():
        raise HTTPException(404, "File is no longer stored")
    return FileResponse(path, filename=a.filename, media_type="application/octet-stream")


@router.get("/events/{event_id}/jobs")
def jobs_page(event_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    jobs = list(db.scalars(select(ProcessingJob).where(ProcessingJob.event_id == ev.id).order_by(ProcessingJob.created_at.desc()).limit(200)))
    usage = list(db.scalars(select(AIUsage).where(AIUsage.event_id == ev.id).order_by(AIUsage.created_at.desc()).limit(200)))
    names = {c.id: c.display_name for c in ev.companies}
    srcs = {s.id: s.original_filename for s in ev.sources}
    return page(request, db, "jobs.html", {"user": user, "event": ev, "jobs": jobs, "usage": usage, "names": names, "srcs": srcs})
