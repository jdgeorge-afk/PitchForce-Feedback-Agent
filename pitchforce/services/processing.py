"""Queueing helpers that encode job ordering (rosters before the files that refer to them)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..jobs.queue import enqueue
from ..models import Event, ProcessingJob, SourceFile

ROSTER_KINDS = ("company_roster", "judge_roster")


def _pending(db: Session, event: Event, kinds: tuple[str, ...] | None = None) -> list[str]:
    q = select(ProcessingJob).where(ProcessingJob.event_id == event.id, ProcessingJob.kind == "ingest_source",
                                    ProcessingJob.status.in_(["queued", "running"]))
    out = []
    for j in db.scalars(q):
        if kinds:
            src = db.get(SourceFile, j.payload.get("source_id"))
            if not src or src.kind not in kinds:
                continue
        out.append(j.id)
    return out


def queue_ingest(db: Session, event: Event, src: SourceFile, user_id: str | None) -> ProcessingJob:
    after = [] if src.kind in ROSTER_KINDS else _pending(db, event, ROSTER_KINDS)
    # Key includes the file hash, so re-uploading identical content does not create a duplicate job.
    return enqueue(db, "ingest_source", f"{src.id}:{src.sha256[:12]}", {"source_id": src.id, "after": after},
                   event_id=event.id, user_id=user_id, rerun=True)


def queue_processing(db: Session, event: Event, user_id: str | None) -> ProcessingJob:
    active = db.scalar(select(ProcessingJob).where(ProcessingJob.event_id == event.id, ProcessingJob.kind == "analyze_event",
                                                   ProcessingJob.status.in_(["queued", "running"])))
    if active:
        return active  # a double click must not start a second run
    after = _pending(db, event)
    stamp = utcnow().strftime("%Y%m%d%H%M%S%f")
    job = enqueue(db, "analyze_event", f"{event.id}:{stamp}", {"after": after}, event_id=event.id, user_id=user_id)
    event.status = "processing"
    db.flush()
    return job
