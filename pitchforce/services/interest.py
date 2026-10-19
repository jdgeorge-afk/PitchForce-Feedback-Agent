"""Investment-interest processing with event context.

- Event filtering: a launch belongs to the event only when its date (in the event timezone) matches
  the event date and, when known, its meeting/webinar ID matches. Filenames are never enough.
- Company assignment: evidence from poll name/topic, question text, a company column, and timing
  against the transcript. Filename-only or conflicting evidence requires review.
- Dedupe: amended submissions supersede earlier ones from the same person within a launch; identical
  repeats are marked duplicates. Anonymous responses are kept and counted without invented contacts.
- Identity: same email with different names, or an email belonging to a different roster person,
  is flagged for review and never merged.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    INTEREST_LABELS,
    AppSetting,
    Company,
    CompanySegment,
    ContactMatch,
    Event,
    Person,
    PollLaunch,
    PollResponse,
    SourceFile,
    Utterance,
)
from ..parsing.polls import DEFAULT_RULES, is_contradictory, normalize_meeting_id
from ..util import norm_company, norm_email, norm_name, normalize_punct
from . import matching

GRACE = timedelta(minutes=15)


def category_rules(db: Session) -> list[tuple[str, str]]:
    s = db.get(AppSetting, "interest_rules")
    if s and s.value.get("rules"):
        return [(r["pattern"], r["category"]) for r in s.value["rules"]]
    return DEFAULT_RULES


# ---------------------------------------------------------------------------
# Event membership
# ---------------------------------------------------------------------------
def _launch_in_event(db: Session, event: Event, pl: PollLaunch) -> None:
    if pl.start_at is None:
        pl.in_event = False
        pl.in_event_reason = f"Launch time {pl.start_raw!r} could not be read."
        matching.open_review(db, event.id, "interest_assignment", f"launch-time:{pl.id}",
                             f"Poll launch {pl.launch_index} has an unreadable start time", pl.in_event_reason,
                             evidence={"ref": pl.source_ref}, target_type="poll_launch", target_id=pl.id)
        return
    ev_wid = normalize_meeting_id(event.webinar_id or "")
    if ev_wid and pl.webinar_id and pl.webinar_id != ev_wid:
        pl.in_event = False
        pl.in_event_reason = f"Meeting/webinar ID {pl.webinar_id_raw} differs from this event's ID {event.webinar_id}."
        return
    same_day = pl.start_at.date() == event.event_date
    pl.in_event = same_day
    if same_day:
        pl.in_event_reason = (f"Launched {pl.start_raw} on the event date" +
                              (f" with matching webinar ID {pl.webinar_id_raw}" if ev_wid and pl.webinar_id else
                               " (no webinar ID to cross-check)" if not ev_wid else ""))
    else:
        pl.in_event_reason = f"Launched {pl.start_raw}, not on the event date {event.event_date:%Y-%m-%d} (historical launch)."
    # Timezone ambiguity: would reading the time as UTC put it on a different side of the event date?
    if pl.timezone_status == "assumed":
        try:
            alt = pl.start_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(event.timezone)).date()
        except Exception:
            alt = pl.start_at.date()
        if (alt == event.event_date) != same_day:
            matching.open_review(
                db, event.id, "timezone", f"launch-tz:{pl.source_file_id}:{pl.launch_index}",
                f"Poll launch {pl.launch_index}: event-date match depends on the file's timezone",
                f"The file does not state a timezone. Read as {event.timezone}, the launch is "
                f"{'on' if same_day else 'not on'} the event date; read as UTC it would be {'on' if not same_day else 'not on'} it.",
                evidence={"start_raw": pl.start_raw}, target_type="poll_launch", target_id=pl.id,
                options=[{"value": "event_tz", "label": f"Times are in {event.timezone}"}, {"value": "utc", "label": "Times are UTC"}],
            )


# ---------------------------------------------------------------------------
# Company assignment
# ---------------------------------------------------------------------------
@dataclass
class Evidence:
    company_id: str
    kind: str
    strength: str  # strong | weak
    detail: str


def _mentions(text: str, companies: list[Company], db: Session) -> list[tuple[Company, str]]:
    t = norm_company(normalize_punct(text or ""))
    out = []
    for c in companies:
        for label in matching.labels_for_company(db, c):
            k = norm_company(label)
            if k and len(k) >= 3 and k in t:
                out.append((c, label))
                break
    return out


def _timing_company(db: Session, event: Event, when: datetime) -> Company | None:
    """Which company's segment (wall-clock transcript) contains the given time, if determinable."""
    tsec = when.hour * 3600 + when.minute * 60 + when.second
    best = None
    for seg in db.scalars(select(CompanySegment).join(Company, Company.id == CompanySegment.company_id)
                          .where(Company.event_id == event.id, CompanySegment.kind.in_(["pitch", "qa", "feedback", "score"]))):
        src = db.get(SourceFile, seg.transcript_source_id) if seg.transcript_source_id else None
        if not src or src.time_basis != "wall_clock" or seg.start_s is None or seg.end_s is None:
            continue
        if seg.start_s - 60 <= tsec <= seg.end_s + 240:
            best = db.get(Company, seg.company_id)
    return best


def _assign_launch_company(db: Session, event: Event, pl: PollLaunch, companies: list[Company], filename: str,
                           responses: list[PollResponse]) -> None:
    if pl.company_status == "confirmed":
        return
    ev: list[Evidence] = []
    for c, label in _mentions(pl.poll_name, companies, db):
        ev.append(Evidence(c.id, "poll name", "strong", f"Poll name “{pl.poll_name}” mentions “{label}”"))
    for c, label in _mentions(pl.source_ref.get("topic", ""), companies, db):
        ev.append(Evidence(c.id, "topic", "weak", f"Meeting topic mentions “{label}”"))
    qs = {r.question_raw for r in responses if r.question_raw}
    for q in qs:
        for c, label in _mentions(q, companies, db):
            ev.append(Evidence(c.id, "question", "strong", f"Poll question mentions “{label}”"))
    if pl.start_at is not None and pl.in_event:
        tc = _timing_company(db, event, pl.start_at)
        if tc:
            ev.append(Evidence(tc.id, "timing", "strong", f"Launched at {pl.start_raw}, during {tc.display_name}'s session in the transcript"))
    for c, label in _mentions(filename, companies, db):
        ev.append(Evidence(c.id, "filename", "weak", f"Filename mentions “{label}” (not sufficient on its own)"))
    pl.company_evidence = {"items": [e.__dict__ for e in ev]}
    strong = {e.company_id for e in ev if e.strength == "strong"}
    weak = {e.company_id for e in ev if e.strength == "weak"}
    if len(strong) == 1 and not (weak - strong):
        pl.company_id = next(iter(strong))
        pl.company_status = "auto"
        return
    pl.company_id = next(iter(strong)) if len(strong) == 1 else (next(iter(weak)) if len(weak) == 1 and not strong else None)
    pl.company_status = "proposed" if pl.company_id else "unassigned"
    if pl.in_event:
        reason = ("Evidence points to different companies." if len(strong | weak) > 1 else
                  "Only the filename suggests a company." if weak and not strong else
                  "No evidence identifies the company.")
        matching.open_review(
            db, event.id, "interest_assignment", f"launch-company:{pl.source_file_id}:{pl.launch_index}:{pl.start_raw}",
            f"Which company is poll launch {pl.launch_index} ({pl.start_raw}) about?", reason,
            evidence={"items": [e.__dict__ for e in ev], "poll_name": pl.poll_name}, severity="high",
            target_type="poll_launch", target_id=pl.id,
        )


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def process_event_interest(db: Session, event: Event) -> dict:
    companies = list(db.scalars(select(Company).where(Company.event_id == event.id)))
    summary: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    sources = list(db.scalars(select(SourceFile).where(SourceFile.event_id == event.id, SourceFile.kind == "interest",
                                                       SourceFile.status != "superseded")))
    for src in sources:
        launches = list(db.scalars(select(PollLaunch).where(PollLaunch.source_file_id == src.id).order_by(PollLaunch.start_at)))
        responses = list(db.scalars(select(PollResponse).where(PollResponse.source_file_id == src.id)))
        for pl in launches:
            _launch_in_event(db, event, pl)
        # Attach responses to launches
        by_launch: dict[str | None, list[PollResponse]] = defaultdict(list)
        for r in responses:
            r.launch_id = _find_launch(r, launches)
            by_launch[r.launch_id].append(r)
        for pl in launches:
            _assign_launch_company(db, event, pl, companies, src.original_filename, by_launch.get(pl.id, []))
        lmap = {pl.id: pl for pl in launches}
        for r in responses:
            pl = lmap.get(r.launch_id) if r.launch_id else None
            if pl is not None:
                r.in_event = pl.in_event
                r.company_id = pl.company_id if pl.company_status in ("auto", "confirmed") else None
            else:
                # No launch table: rely on the submission date (and meeting ID when present).
                mid = normalize_meeting_id(r.source_ref.get("meeting_id_raw", ""))
                ev_wid = normalize_meeting_id(event.webinar_id or "")
                if r.submitted_at is None:
                    r.in_event = False
                    r.review_reason = "Submission time missing; cannot confirm this response belongs to the event."
                else:
                    r.in_event = r.submitted_at.date() == event.event_date and (not ev_wid or not mid or mid == ev_wid)
                r.company_id = None
            # Row-level company column overrides the launch assignment when it names a company.
            row_company = r.source_ref.get("company", "")
            if row_company:
                m = matching.match_company(db, event, row_company)
                if m.certain:
                    r.company_id = m.target_id
        if not launches:
            _assign_file_company(db, event, src, companies, [r for r in responses if r.in_event])
        _dedupe(db, event, [r for r in responses if r.in_event])
        _conflicts(db, event, [r for r in responses if r.in_event])
        s = summary[src.id]
        s["in_event"] = sum(1 for r in responses if r.in_event)
        s["excluded_other_launches"] = sum(1 for r in responses if not r.in_event)
        s["launches_in_event"] = sum(1 for pl in launches if pl.in_event)
    db.flush()
    return {k: dict(v) for k, v in summary.items()}


def _find_launch(r: PollResponse, launches: list[PollLaunch]) -> str | None:
    if not launches:
        return None
    lr = (r.source_ref.get("launch_raw") or "").strip()
    if lr.isdigit():
        for pl in launches:
            if pl.launch_index == int(lr):
                return pl.id
    mid = normalize_meeting_id(r.source_ref.get("meeting_id_raw", ""))
    cands = [pl for pl in launches if not mid or not pl.webinar_id or pl.webinar_id == mid]
    if r.submitted_at is None:
        return None
    cands = sorted([pl for pl in cands if pl.start_at is not None], key=lambda p: p.start_at)
    for i, pl in enumerate(cands):
        upper = pl.end_at + GRACE if pl.end_at else (cands[i + 1].start_at if i + 1 < len(cands) else pl.start_at + timedelta(hours=6))
        if i + 1 < len(cands):
            upper = min(upper, cands[i + 1].start_at)
        if pl.start_at - timedelta(minutes=1) <= r.submitted_at <= upper:
            return pl.id
    return None


def _assign_file_company(db: Session, event: Event, src: SourceFile, companies: list[Company], responses: list[PollResponse]) -> None:
    # Rows that name a company the roster does not know: ask about that label specifically.
    unknown_labels: dict[str, int] = defaultdict(int)
    for r in responses:
        if not r.company_id and r.source_ref.get("company"):
            unknown_labels[r.source_ref["company"]] += 1
    for label, n in unknown_labels.items():
        m = matching.match_company(db, event, label)
        matching.open_review(
            db, event.id, "interest_assignment", f"row-company:{src.id}:{norm_company(label)}",
            f"{n} poll response(s) name “{label}”, which is not a confirmed roster company",
            m.reason + ". Confirm which presenting company this refers to (an alias is recorded only when you confirm).",
            evidence={"label": label, "proposed_company_id": m.target_id, "file": src.original_filename}, severity="high",
            target_type="company_label", target_id=m.target_id or "",
            options=[{"value": c.id, "label": c.display_name} for c in companies],
        )
    responses = [r for r in responses if not r.source_ref.get("company")]
    if not responses or all(r.company_id for r in responses):
        return
    confirmed = (src.meta or {}).get("company_confirmed")
    if confirmed:
        for r in responses:
            r.company_id = r.company_id or confirmed
        return
    ev = [(c.id, "question", lbl) for q in {r.question_raw for r in responses} for c, lbl in _mentions(q, companies, db)]
    strong = {cid for cid, *_ in ev}
    if len(strong) == 1:
        for r in responses:
            r.company_id = r.company_id or next(iter(strong))
        return
    fname = _mentions(src.original_filename, companies, db)
    matching.open_review(
        db, event.id, "interest_assignment", f"file-company:{src.id}",
        f"Which company are the responses in “{src.original_filename}” about?",
        "The file has no launch table and the questions do not name a company." + (" Only the filename suggests one." if fname else ""),
        evidence={"filename_mentions": [c.display_name for c, _ in fname]}, severity="high",
        target_type="source_file", target_id=src.id,
    )


def _person_key(r: PollResponse) -> str | None:
    if r.is_anonymous:
        return None
    return norm_email(r.email) or ("name:" + norm_name(r.name) if r.name else None)


def _dedupe(db: Session, event: Event, responses: list[PollResponse]) -> None:
    for r in responses:
        r.superseded_by_id = None
        r.duplicate_of_id = None
    groups: dict[tuple, list[PollResponse]] = defaultdict(list)
    for r in responses:
        k = _person_key(r)
        if k:
            groups[(r.launch_id or r.source_file_id, r.question_raw, k)].append(r)
    for rows in groups.values():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: (r.submitted_at or datetime.min, r.original_response_id))
        latest = rows[-1]
        for r in rows[:-1]:
            if normalize_punct(r.answer_raw).strip() == normalize_punct(latest.answer_raw).strip():
                r.duplicate_of_id = latest.id
            else:
                r.superseded_by_id = latest.id
                r.review_reason = (r.review_reason + " " if r.review_reason else "") + "Superseded by a later amended submission from the same person."


def _conflicts(db: Session, event: Event, responses: list[PollResponse]) -> None:
    roster = list(db.scalars(select(Person).where(Person.event_id == event.id)))
    by_email: dict[str, set[str]] = defaultdict(set)
    for r in responses:
        if r.email and r.name:
            by_email[r.email].add(norm_name(r.name))
    for r in responses:
        cats = {c.category for c in r.choices}
        r.has_conflict = is_contradictory(cats)
        if r.has_conflict:
            matching.open_review(
                db, event.id, "interest_conflict", f"contradiction:{r.source_file_id}:{r.original_response_id}:{r.source_ref.get('ref')}",
                f"Response {r.original_response_id or ''} selects both “not interested” and other options",
                f"Choices: {r.answer_raw!r}. Both are preserved; decide how to treat it for outreach.",
                company_id=r.company_id, evidence={"ref": r.source_ref.get("ref"), "choices": [c.choice_raw for c in r.choices]},
                target_type="poll_response", target_id=r.id,
            )
        if r.email and len(by_email[r.email]) > 1:
            matching.open_review(
                db, event.id, "contact", f"shared-email:{r.email}",
                "Several different names used the same email address in poll responses",
                "Responses are kept separate; they are not merged into one person.",
                evidence={"names": sorted(by_email[r.email])}, target_type="poll_response", target_id=r.id,
            )
        # Email belongs to a roster person with a different name -> conflict, never merge.
        if r.email:
            for p in roster:
                if p.email and norm_email(p.email) == r.email and r.name and norm_name(p.display_name) != norm_name(r.name):
                    cm = db.scalar(select(ContactMatch).where(ContactMatch.response_id == r.id, ContactMatch.person_id == p.id))
                    if not cm:
                        db.add(ContactMatch(event_id=event.id, response_id=r.id, person_id=p.id, subject_label=r.name,
                                            status="conflict",
                                            reason=f"Response name “{r.name}” uses the email that the roster assigns to “{p.display_name}”.",
                                            evidence={"response_ref": r.source_ref.get("ref"), "roster_ref": p.source_ref}))
                    matching.open_review(
                        db, event.id, "contact", f"email-conflict:{r.id}:{p.id}",
                        f"Response {r.original_response_id}: name “{r.name}” with an email that belongs to “{p.display_name}” on the roster",
                        "The original response is preserved. The two people are not merged automatically.",
                        company_id=r.company_id, evidence={"response_ref": r.source_ref.get("ref")},
                        target_type="poll_response", target_id=r.id,
                        options=[{"value": "keep_separate", "label": "Keep as separate people"},
                                 {"value": "same_person", "label": "Same person (record alias)"}],
                    )


# ---------------------------------------------------------------------------
# Summaries for reports and exports
# ---------------------------------------------------------------------------
@dataclass
class InterestSummary:
    status: str  # available | missing_source | not_assigned | needs_review
    note: str
    respondents: int = 0
    anonymous: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    responses: list[PollResponse] = field(default_factory=list)
    open_reviews: int = 0


def summarize(db: Session, event: Event, company: Company) -> InterestSummary:
    sources = list(db.scalars(select(SourceFile).where(SourceFile.event_id == event.id, SourceFile.kind == "interest",
                                                       SourceFile.status == "parsed")))
    if not sources:
        return InterestSummary("missing_source", "No investment-interest file was uploaded for this event. Interest data is missing (not zero).")
    rows = list(db.scalars(select(PollResponse).where(PollResponse.company_id == company.id, PollResponse.in_event.is_(True))))
    pending = list(db.scalars(select(PollLaunch).where(PollLaunch.event_id == event.id, PollLaunch.in_event.is_(True),
                                                       PollLaunch.company_status.in_(["proposed", "unassigned"]))))
    if not rows:
        if pending:
            return InterestSummary("needs_review", "Some event poll launches are not yet assigned to a company; resolve them in the review queue.",
                                   open_reviews=len(pending))
        return InterestSummary("not_assigned", "No event-specific poll responses were found for this company in the uploaded files. "
                               "This means no data, not zero interest.")
    active = [r for r in rows if not r.superseded_by_id and not r.duplicate_of_id]
    people = set()
    anon = 0
    by_cat: dict[str, set] = defaultdict(set)
    for r in active:
        key = _person_key(r) or f"anon:{r.id}"
        if r.is_anonymous:
            anon += 1
        people.add(key)
        for c in {c.category for c in r.choices} or {"unknown"}:
            by_cat[c].add(key)
    return InterestSummary(
        "available", "", respondents=len(people), anonymous=anon,
        by_category={k: len(v) for k, v in sorted(by_cat.items(), key=lambda kv: list(INTEREST_LABELS).index(kv[0]))},
        responses=sorted(rows, key=lambda r: (r.submitted_at or datetime.min)), open_reviews=len(pending),
    )


SUGGESTED_ACTION = {
    "invest": "Offer a specific next step: a call to discuss terms and diligence materials.",
    "meeting": "Propose two or three specific times for a follow-up call.",
    "due_diligence": "Send the diligence materials requested and confirm what they need next.",
    "more_info": "Send the requested information promptly; ask what would help them evaluate further.",
    "free_resource": "Reply to learn what the resource is and whether it fits your current needs.",
    "introduction": "Thank them and say specifically who you would like to be introduced to and why.",
    "mentoring": "Reply to explore a mentoring conversation on a specific topic.",
    "unknown": "Review the original response wording before reaching out.",
}

UTTERANCE = Utterance  # re-export for typing in callers
