"""Per-company processing and staleness tracking.

``process_company`` is what the background job runs for one company. It is idempotent: re-running
replaces derived rows (keeping operator decisions) and only regenerates report sections whose inputs
changed. Outputs (PDF/DOCX/XLSX/ZIP) record the hash of the inputs they were built from; anything
whose hash no longer matches is shown as stale.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Artifact, Clip, Company, Event, PollResponse, ReportSection
from ..util import stable_hash


def mark_sections_stale(db: Session, company: Company, keys: list[str], reason: str) -> None:
    for s in db.scalars(select(ReportSection).where(ReportSection.company_id == company.id, ReportSection.key.in_(keys))):
        s.stale = True
        s.stale_reason = reason


def package_input_hash(db: Session, company: Company) -> str:
    secs = [(s.key, s.text) for s in db.scalars(select(ReportSection).where(ReportSection.company_id == company.id)
                                               .order_by(ReportSection.position))]
    clip = db.scalar(select(Clip).where(Clip.company_id == company.id, Clip.variant == "core"))
    rows = [(r.id, r.outreach_status, r.outreach_note, r.company_id, r.in_event, r.superseded_by_id, r.duplicate_of_id,
             [(c.category, c.choice_raw) for c in r.choices])
            for r in db.scalars(select(PollResponse).where(PollResponse.company_id == company.id).order_by(PollResponse.id))]
    return stable_hash([secs, clip.rendered_hash if clip else None, clip.share_url if clip else None, rows])


def stale_artifacts(db: Session, company: Company) -> list[Artifact]:
    h = package_input_hash(db, company)
    out = []
    for a in db.scalars(select(Artifact).where(Artifact.company_id == company.id, Artifact.is_current.is_(True))):
        if a.kind == "clip_mp4":
            clip = db.scalar(select(Clip).where(Clip.company_id == company.id, Clip.variant == (a.meta or {}).get("variant", "core")))
            if clip and clip.boundary_hash != a.input_hash:
                out.append(a)
        elif a.input_hash != h or a.stale:
            out.append(a)
    return out


def process_company(db: Session, event: Event, company: Company, progress=lambda *_: None, provider=None) -> dict:
    from ..analysis.heuristics import THEME_LABELS  # noqa: F401
    from ..analysis.pipeline import analyze_company
    from ..analysis.providers import get_provider
    from ..reports.compose import Evidence, compose
    from . import calendar, clips, scoring

    provider = provider or get_provider()
    progress("Extracting questions and feedback", 0.1)
    res = analyze_company(db, event, company, provider=provider, progress=progress)
    db.commit()  # short transactions: other companies' jobs and the worker can write in between
    progress("Calculating score", 0.55)
    scoring.compute_for_company(db, event, company)
    db.commit()
    progress("Locating video clip", 0.65)
    clips.update_candidate(db, event, company, "core")
    if event.include_score_in_clip:
        clips.update_candidate(db, event, company, "with_score")
    db.commit()
    progress("Matching learning opportunities", 0.75)
    ev = Evidence(db, event, company)
    calendar.recommend(db, company, ev.theme_keys())
    db.commit()
    progress("Writing report sections", 0.85)
    actions = compose(db, event, company, provider=provider)
    company.status = "ready"
    company.last_error = None
    db.flush()
    return {**res, "sections": actions}
