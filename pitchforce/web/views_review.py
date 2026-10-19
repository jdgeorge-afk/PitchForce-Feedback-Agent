"""Review queue: uncertain boundaries, names, scores, contacts, interest assignment, source conflicts.

Every decision is stored (ReviewDecision + audit log) with its scope. Event-scoped corrections apply
to this event only; "global" must be chosen explicitly. Decisions are re-applied on regeneration.
"""
from __future__ import annotations

from datetime import timezone as dt_tz
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import record
from ..auth import check_csrf, current_user, require_event
from ..db import get_db, utcnow
from ..jobs.queue import enqueue
from ..models import (
    Company,
    CompanySegment,
    ContactMatch,
    EntityRelationship,
    Event,
    JudgeAttendance,
    Person,
    PollLaunch,
    PollResponse,
    ReviewDecision,
    ReviewItem,
    Score,
    SourceFile,
    User,
)
from ..services import matching
from ..services.ingest import resolve_score_identities
from ..services.interest import process_event_interest
from ..services.processing import queue_processing
from ..services.scoring import compute_for_company
from .common import flash, page

router = APIRouter()
SEV_ORDER = {"high": 0, "medium": 1, "low": 2}
KIND_TITLES = {
    "company_match": "Company and presenter matches", "related_entity": "Related companies and names", "boundary": "Session boundaries",
    "alignment": "Video alignment", "score": "Scores and judge attendance", "announced_total": "Announced totals",
    "contact": "Contacts and identities", "interest_assignment": "Interest poll assignment", "interest_conflict": "Contradictory poll answers",
    "timezone": "Timezone interpretation", "source_conflict": "Source issues", "missing_source": "Missing sources",
    "clip_validation": "Clip checks", "person_match": "People", "speaker": "Speakers", "question": "Questions", "feedback": "Feedback",
    "best_practice_source": "Best-practice documents",
}


@router.get("/events/{event_id}/review")
def review_page(event_id: str, request: Request, show: str = "open", db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    q = select(ReviewItem).where(ReviewItem.event_id == ev.id)
    if show == "open":
        q = q.where(ReviewItem.status == "open")
    items = sorted(db.scalars(q), key=lambda i: (SEV_ORDER.get(i.severity, 3), i.kind, i.created_at))
    groups: dict[str, list[ReviewItem]] = {}
    for it in items:
        groups.setdefault(it.kind, []).append(it)
    companies = list(db.scalars(select(Company).where(Company.event_id == ev.id).order_by(Company.slot_order)))
    judges = list(db.scalars(select(Person).where(Person.event_id == ev.id, Person.role == "judge").order_by(Person.display_name)))
    return page(request, db, "review.html", {"user": user, "event": ev, "groups": groups, "titles": KIND_TITLES, "show": show,
                                             "companies": companies, "judges": judges, "cmap": {c.id: c for c in companies}})


def apply_relationship(db: Session, ev: Event, r: EntityRelationship, relation: str, user: User, scope: str = "event") -> None:
    from ..models import RELATION_TYPES

    if relation not in RELATION_TYPES:
        raise HTTPException(400, "Unknown relationship")
    c = db.get(Company, r.company_id) if r.company_id else None
    r.relation = relation
    r.status = "rejected" if relation == "unrelated" else "confirmed"
    r.scope = scope
    r.confirmed_by, r.confirmed_at = user.id, utcnow()
    if c is not None:
        if relation in ("same_company", "trading_name", "product_name", "alternate_name"):
            # A confirmed association used for matching. It is not a statement about legal structure.
            matching.add_company_alias(db, c, r.subject_label, "operator", status="confirmed", scope=scope,
                                       note=f"Confirmed as {relation.replace('_', ' ')} (association only, not legal structure)",
                                       user_id=user.id)
        elif relation == "unrelated":
            matching.add_company_alias(db, c, r.subject_label, "operator", status="rejected", scope=scope,
                                       note="Operator: not this company", user_id=user.id)


def _decide(db: Session, ev: Event, item: ReviewItem, decision: str, user: User, note: str = "", scope: str = "event", payload=None):
    item.status = "dismissed" if decision == "dismiss" else "resolved"
    item.resolved_at = utcnow()
    item.evidence = {**(item.evidence or {}), "resolution": decision}
    db.add(ReviewDecision(review_item_id=item.id, event_id=ev.id, decision=decision, scope=scope, note=note, payload=payload or {},
                          user_id=user.id))
    record(db, "review:" + decision, "review_item", item.id, ev.id, after={"kind": item.kind, "title": item.title, "note": note},
           user_id=user.id)


def _reprocess_company(db: Session, ev: Event, company_id: str | None, user: User) -> None:
    if company_id:
        enqueue(db, "process_company", company_id, {"company_id": company_id}, event_id=ev.id, company_id=company_id,
                user_id=user.id, rerun=True)


@router.post("/events/{event_id}/review/{item_id}", dependencies=[Depends(check_csrf)])
async def resolve(event_id: str, item_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    item = db.get(ReviewItem, item_id)
    if not item or item.event_id != ev.id:
        raise HTTPException(404, "Review item not found")
    f = await request.form()
    decision = f.get("decision", "")
    note = (f.get("note") or "")[:1000]
    scope = "global" if f.get("scope") == "global" else "event"
    msg = "Saved."

    if decision == "dismiss":
        _decide(db, ev, item, "dismiss", user, note)
        msg = "Dismissed."
    elif decision == "reopen":
        item.status, item.resolved_at = "open", None
        msg = "Reopened."
    elif item.kind == "company_match":
        c = db.get(Company, item.target_id) if item.target_id else None
        if c is None:
            _decide(db, ev, item, decision or "acknowledged", user, note)
        elif decision == "confirm":
            for a in c.aliases:
                if a.status == "proposed":
                    a.status, a.scope, a.created_by = "confirmed", scope, user.id
                    a.note = (a.note + " " if a.note else "") + "Confirmed association (not proof of legal or product structure)."
            for p in db.scalars(select(Person).where(Person.company_id == c.id, Person.role == "presenter")):
                for a in p.aliases:
                    if a.status == "proposed":
                        a.status, a.scope, a.created_by = "confirmed", scope, user.id
            for seg in db.scalars(select(CompanySegment).where(CompanySegment.company_id == c.id, CompanySegment.status == "needs_review")):
                seg.status = "candidate"
            _decide(db, ev, item, "confirm", user, note, scope)
            queue_processing(db, ev, user.id)
            msg = f"Confirmed. Aliases recorded for {c.display_name}; processing re-runs to apply them."
        elif decision == "reject":
            for a in c.aliases:
                if a.status == "proposed" and a.source == "transcript":
                    a.status, a.created_by = "rejected", user.id
            for p in db.scalars(select(Person).where(Person.company_id == c.id, Person.role == "presenter")):
                for a in p.aliases:
                    if a.status == "proposed":
                        a.status = "rejected"
            _decide(db, ev, item, "reject", user, note, scope)
            queue_processing(db, ev, user.id)
            msg = "Recorded as a different company. Assign the transcript lines manually if needed."
    elif item.kind == "related_entity":
        r = db.get(EntityRelationship, item.target_id) if item.target_id else None
        if r is None:
            raise HTTPException(404, "Relationship not found")
        apply_relationship(db, ev, r, decision, user, scope)
        _decide(db, ev, item, decision, user, note, scope)
        _reprocess_company(db, ev, r.company_id, user)
        msg = f"Recorded “{r.subject_label}” as {decision.replace('_', ' ')}."
    elif item.kind == "score" and item.target_type == "judge_absence" or (item.kind == "score" and item.dedupe_key.startswith("absence:")):
        pid = f.get("person_id") or item.target_id
        if decision == "confirm_absent":
            p = db.get(Person, pid) if pid else None
            if not p or p.event_id != ev.id:
                flash(request, "Choose which judge was absent.", "error")
                return RedirectResponse(f"/events/{ev.id}/review", status_code=303)
            att = db.scalar(select(JudgeAttendance).where(JudgeAttendance.event_id == ev.id, JudgeAttendance.person_id == p.id))
            att = att or JudgeAttendance(event_id=ev.id, person_id=p.id)
            att.status, att.source, att.confirmed_by = "absent_confirmed", "operator", user.id
            att.evidence = {"review_item": item.id, **(item.evidence or {})}
            db.add(att)
            db.flush()
            msg = f"{p.display_name} confirmed absent. Scores recalculated."
        else:
            msg = "Recorded: absence not confirmed (no imputation)."
        for c in ev.companies:
            compute_for_company(db, ev, c)
            _reprocess_company(db, ev, c.id, user)
        _decide(db, ev, item, decision, user, note)
    elif item.kind == "score":
        s = db.get(Score, item.target_id) if item.target_id else None
        target = f.get("choice")
        if s is not None and target:
            if item.target_type == "score_company_label":
                c = db.get(Company, target)
                if c and c.event_id == ev.id:
                    matching.add_company_alias(db, c, s.company_label_raw, "operator", status="confirmed", scope=scope,
                                               note="Score sheet name", user_id=user.id)
            elif item.target_type == "score_judge_label":
                p = db.get(Person, target)
                if p and p.event_id == ev.id:
                    matching.add_person_alias(db, p, s.judge_label_raw, "operator", status="confirmed", scope=scope,
                                              note="Score sheet name", user_id=user.id)
            db.flush()
            resolve_score_identities(db, ev)
            for c in ev.companies:
                compute_for_company(db, ev, c)
                _reprocess_company(db, ev, c.id, user)
            msg = "Matched; scores recalculated."
        _decide(db, ev, item, decision or "matched", user, note, scope)
    elif item.kind in ("interest_assignment",):
        choice = f.get("choice")
        c = db.get(Company, choice) if choice else None
        if c is None or c.event_id != ev.id:
            flash(request, "Choose a company.", "error")
            return RedirectResponse(f"/events/{ev.id}/review", status_code=303)
        if item.target_type == "poll_launch":
            pl = db.get(PollLaunch, item.target_id)
            pl.company_id, pl.company_status = c.id, "confirmed"
        elif item.target_type == "source_file":
            src = db.get(SourceFile, item.target_id)
            src.meta = {**(src.meta or {}), "company_confirmed": c.id}
        elif item.target_type == "company_label":
            label = (item.evidence or {}).get("label", "")
            if label:
                matching.add_company_alias(db, c, label, "operator", status="confirmed", scope=scope,
                                           note="Poll response company name", user_id=user.id)
        db.flush()
        process_event_interest(db, ev)
        _decide(db, ev, item, f"assigned:{c.id}", user, note, scope)
        _reprocess_company(db, ev, c.id, user)
        msg = f"Assigned to {c.display_name}."
    elif item.kind == "timezone":
        pl = db.get(PollLaunch, item.target_id)
        if pl:
            if decision == "utc":
                tz = ZoneInfo(ev.timezone)
                for x in db.scalars(select(PollLaunch).where(PollLaunch.source_file_id == pl.source_file_id)):
                    for attr in ("start_at", "end_at"):
                        v = getattr(x, attr)
                        if v:
                            setattr(x, attr, v.replace(tzinfo=dt_tz.utc).astimezone(tz).replace(tzinfo=None))
                    x.timezone_assumed, x.timezone_status = "UTC", "confirmed"
                for r in db.scalars(select(PollResponse).where(PollResponse.source_file_id == pl.source_file_id)):
                    if r.submitted_at:
                        r.submitted_at = r.submitted_at.replace(tzinfo=dt_tz.utc).astimezone(tz).replace(tzinfo=None)
            else:
                for x in db.scalars(select(PollLaunch).where(PollLaunch.source_file_id == pl.source_file_id)):
                    x.timezone_status = "confirmed"
            db.flush()
            process_event_interest(db, ev)
        _decide(db, ev, item, decision, user, note)
        msg = "Timezone recorded; poll responses re-filtered."
    elif item.kind == "interest_conflict":
        r = db.get(PollResponse, item.target_id)
        if r and decision == "treat_not_interested":
            r.outreach_status = "Do not contact"
            r.review_reason = (r.review_reason + " " if r.review_reason else "") + "Operator: treat as not interested."
        _decide(db, ev, item, decision or "keep", user, note)
        if r:
            _reprocess_company(db, ev, r.company_id, user)
    elif item.kind == "contact":
        r = db.get(PollResponse, item.target_id) if item.target_id else None
        if r is not None:
            for cm in db.scalars(select(ContactMatch).where(ContactMatch.response_id == r.id)):
                if decision == "same_person":
                    cm.status = "confirmed"
                    p = db.get(Person, cm.person_id)
                    if p:
                        matching.add_person_alias(db, p, r.name, "operator", status="confirmed", scope="event",
                                                  note="Poll response name confirmed by operator", user_id=user.id)
                elif decision == "keep_separate":
                    cm.status = "rejected"
                cm.decided_by = user.id
        _decide(db, ev, item, decision or "acknowledged", user, note)
    elif item.kind == "boundary":
        seg = db.get(CompanySegment, item.target_id) if item.target_id else None
        if seg is not None and decision == "confirm":
            seg.status, seg.updated_by = "confirmed", user.id
            _reprocess_company(db, ev, seg.company_id, user)
        _decide(db, ev, item, decision or "acknowledged", user, note)
    else:
        _decide(db, ev, item, decision or "acknowledged", user, note, scope)
    db.commit()
    flash(request, msg, "success")
    back = f.get("back")
    return RedirectResponse(back if back and back.startswith("/events/") else f"/events/{ev.id}/review", status_code=303)
