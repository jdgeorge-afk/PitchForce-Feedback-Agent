"""Source inventory and "what can still be produced" explanations for incomplete input."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Company, Event, Person, Score, SourceFile
from .sources import KIND_LABELS

CHECKLIST = [
    ("company_roster", True, "Needed to name companies and presenters. You can also type the roster in by hand."),
    ("transcript", True, "Needed for questions, feedback, and clip boundaries."),
    ("video", False, "Needed for video clips. Without it, reports say the clip is unavailable."),
    ("judge_roster", False, "Gives judge names, affiliations and attendance. Without it, names come from the transcript and contact fields stay blank."),
    ("scores", False, "Individual judge scores let totals be verified. Without them, announced totals are shown as announced, not verified."),
    ("interest", False, "Investment-interest poll exports. Without them, interest is reported as missing data (not zero)."),
    ("guidelines", False, "Event-specific pitch guidelines (optional)."),
    ("deck", False, "Pitch decks (optional; stored for reference, not visually reviewed)."),
]


def inventory(db: Session, event: Event) -> dict:
    srcs = list(db.scalars(select(SourceFile).where(SourceFile.event_id == event.id).order_by(SourceFile.created_at)))
    by_kind: dict[str, list[SourceFile]] = {}
    for s in srcs:
        k = "scores" if s.kind == "chat" else s.kind
        by_kind.setdefault(k, []).append(s)
    rows = []
    for kind, required, why in CHECKLIST:
        files = by_kind.get(kind, [])
        ok = [f for f in files if f.status == "parsed"]
        failed = [f for f in files if f.status == "failed"]
        status = "present" if ok else ("failed" if failed else ("processing" if files else ("missing_required" if required else "missing")))
        if kind == "company_roster" and not files and db.scalar(select(func.count(Company.id)).where(Company.event_id == event.id)):
            status, why = "present", "Entered manually."
        rows.append({"kind": kind, "label": KIND_LABELS[kind], "status": status, "files": files, "why": why})
    has = {r["kind"]: r["status"] == "present" for r in rows}
    s = get_settings()
    caps = []
    if has["transcript"]:
        caps.append(("Founder feedback reports, questions and panel feedback", "available", ""))
    elif has["video"] and s.transcription_configured:
        caps.append(("Founder feedback reports", "action", "No transcript yet: start transcription of the video (Sources page)."))
    elif has["video"]:
        caps.append(("Founder feedback reports", "unavailable", "Upload a transcript (TXT/VTT/SRT). A transcription provider is not configured."))
    else:
        caps.append(("Founder feedback reports", "unavailable", "Upload a transcript."))
    if has["video"] and has["transcript"]:
        caps.append(("Video clips", "available", "Clip boundaries come from the transcript and are checked in the player."))
    else:
        caps.append(("Video clips", "unavailable", "No recording uploaded; reports will show the clip as unavailable." if not has["video"]
                     else "A transcript is needed to find each company's segment."))
    n_scores = db.scalar(select(func.count(Score.id)).where(Score.event_id == event.id, Score.status == "valid")) or 0
    if n_scores:
        caps.append(("Verified score totals", "available", f"{n_scores} valid individual scores loaded."))
    else:
        caps.append(("Verified score totals", "limited", "No individual scores: announced totals from the transcript are shown as announced, not verified."))
    caps.append(("Investor follow-up workbooks", "available" if has["interest"] else "limited",
                 "" if has["interest"] else "No interest export: interest is marked missing (not zero)."))
    judges = db.scalar(select(func.count(Person.id)).where(Person.event_id == event.id, Person.role == "judge")) or 0
    if not has["judge_roster"]:
        caps.append(("Judge details", "limited", "No judge roster: names come from the transcript; contact fields are left blank."))
    elif judges != event.expected_judge_count:
        caps.append(("Judge details", "limited", f"{judges} judges on the roster; the event expects {event.expected_judge_count}."))
    return {"rows": rows, "capabilities": caps, "sources": srcs}
