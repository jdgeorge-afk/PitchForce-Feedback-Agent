"""Durable database-backed job queue.

Why a DB queue: one less service to run, survives restarts, and jobs are visible to the UI in the
same database. Claiming uses a conditional UPDATE so it is safe with several worker processes on
PostgreSQL and with SQLite (single writer at a time).

- Idempotency: every job has a unique ``idempotency_key``. Enqueuing the same key returns the
  existing job instead of creating a duplicate. Handlers write outputs keyed by stable IDs/hashes,
  so a retried job overwrites rather than duplicates.
- Leases: a running job holds a lease that a heartbeat extends. If the worker dies, the lease
  expires and another worker reclaims the job (counted as a new attempt).
- Retries: failures are retried with backoff up to ``max_attempts``; ``PermanentJobError`` fails at once.
- Cancellation: ``cancel_requested`` is checked at every progress update.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal, utcnow
from ..models import ProcessingJob


class JobCancelled(Exception):
    pass


class PermanentJobError(Exception):
    """An error that retrying cannot fix (bad input). Shown to the operator as-is."""


HANDLERS: dict[str, Callable[["JobContext"], dict | None]] = {}


def _short_lock_timeout(db: Session, ms: int = 300) -> None:
    if db.bind is not None and db.bind.dialect.name == "sqlite":
        db.execute(text(f"PRAGMA busy_timeout={int(ms)}"))


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn

    return deco


def enqueue(
    db: Session,
    kind: str,
    key: str,
    payload: dict[str, Any] | None = None,
    event_id: str | None = None,
    company_id: str | None = None,
    parent_id: str | None = None,
    user_id: str | None = None,
    rerun: bool = False,
    max_attempts: int | None = None,
) -> ProcessingJob:
    """Create a job, or return the existing job with the same idempotency key.

    ``rerun=True`` re-queues an existing finished job (operator pressed "Run again" or inputs changed).
    """
    idem = f"{kind}:{key}"
    job = db.scalar(select(ProcessingJob).where(ProcessingJob.idempotency_key == idem))
    if job:
        if rerun and job.status in ("succeeded", "failed", "cancelled"):
            job.status = "queued"
            job.stage = "Waiting to start"
            job.progress = 0.0
            job.error = None
            job.attempts = 0
            job.cancel_requested = False
            job.run_after = utcnow()
            job.payload = payload or job.payload
            job.parent_id = parent_id or job.parent_id
            job.finished_at = None
            db.commit()
        return job
    job = ProcessingJob(
        kind=kind,
        idempotency_key=idem,
        payload=payload or {},
        event_id=event_id,
        company_id=company_id,
        parent_id=parent_id,
        created_by=user_id,
        max_attempts=max_attempts or get_settings().job_max_attempts,
    )
    db.add(job)
    db.commit()
    return job


def retry(db: Session, job: ProcessingJob) -> None:
    if job.status in ("failed", "cancelled"):
        job.status = "queued"
        job.attempts = 0
        job.error = None
        job.cancel_requested = False
        job.stage = "Waiting to start (retry requested)"
        job.progress = 0
        job.run_after = utcnow()
        job.finished_at = None
        db.commit()


def cancel(db: Session, job: ProcessingJob) -> None:
    db.refresh(job)  # act on the current state, not a stale copy
    if job.status == "queued":
        job.status = "cancelled"
        job.stage = "Cancelled before start"
        job.finished_at = utcnow()
    elif job.status == "running":
        job.cancel_requested = True
        job.stage = "Cancelling…"
    for child in db.scalars(select(ProcessingJob).where(ProcessingJob.parent_id == job.id)):
        if child.status in ("queued", "running"):
            cancel(db, child)
    db.commit()


def claim_next(worker_id: str, kinds: list[str] | None = None) -> str | None:
    """Atomically claim one runnable job; returns its id or None."""
    s = get_settings()
    now = utcnow()
    with SessionLocal() as db:
        runnable = or_(
            (ProcessingJob.status == "queued") & (ProcessingJob.run_after <= now),
            (ProcessingJob.status == "running") & (ProcessingJob.lease_until < now),
        )
        q = select(ProcessingJob.id).where(runnable).order_by(ProcessingJob.created_at).limit(25)
        if kinds:
            q = q.where(ProcessingJob.kind.in_(kinds))
        for (job_id,) in db.execute(q).all():
            after = (db.get(ProcessingJob, job_id).payload or {}).get("after") or []
            if after:
                pending = db.scalar(select(ProcessingJob.id).where(ProcessingJob.id.in_(after),
                                                                   ProcessingJob.status.in_(["queued", "running"])).limit(1))
                if pending:
                    continue  # wait for prerequisite jobs (e.g. rosters before transcripts)
            res = db.execute(
                update(ProcessingJob)
                .where(ProcessingJob.id == job_id)
                .where(runnable)
                .values(
                    status="running",
                    worker_id=worker_id,
                    lease_until=now + timedelta(seconds=s.job_lease_seconds),
                    attempts=ProcessingJob.attempts + 1,
                    started_at=now,
                )
            )
            db.commit()
            if res.rowcount == 1:
                return job_id
    return None


@dataclass
class JobContext:
    job_id: str
    worker_id: str
    kind: str
    payload: dict
    event_id: str | None
    company_id: str | None
    attempt: int

    def progress(self, stage: str, fraction: float | None = None) -> None:
        """Report the actual stage; extends the lease; raises JobCancelled if requested.

        Progress writes are best-effort: on SQLite the handler's own open transaction can hold the
        write lock, so a progress update that cannot get the lock quickly is skipped (never fatal)."""
        s = get_settings()
        with SessionLocal() as db:
            job = db.get(ProcessingJob, self.job_id)
            if job is None or job.cancel_requested or job.status == "cancelled":
                raise JobCancelled()
            try:
                _short_lock_timeout(db)
                db.execute(update(ProcessingJob).where(ProcessingJob.id == self.job_id).values(
                    stage=stage[:120], lease_until=utcnow() + timedelta(seconds=s.job_lease_seconds),
                    **({"progress": max(0.0, min(1.0, fraction))} if fraction is not None else {})))
                db.commit()
            except OperationalError:
                db.rollback()
            finally:
                _short_lock_timeout(db, 30000)

    def check_cancel(self) -> None:
        with SessionLocal() as db:
            job = db.get(ProcessingJob, self.job_id)
            if job is None or job.cancel_requested or job.status == "cancelled":
                raise JobCancelled()

    def heartbeat(self) -> bool:
        """Extend the lease. Returns False only when the job is no longer ours."""
        s = get_settings()
        with SessionLocal() as db:
            try:
                _short_lock_timeout(db)
                res = db.execute(
                    update(ProcessingJob)
                    .where(ProcessingJob.id == self.job_id, ProcessingJob.worker_id == self.worker_id, ProcessingJob.status == "running")
                    .values(lease_until=utcnow() + timedelta(seconds=s.job_lease_seconds))
                )
                db.commit()
                return res.rowcount == 1
            except OperationalError:
                db.rollback()
                return True  # busy; try again at the next beat
            finally:
                _short_lock_timeout(db, 30000)

    def add_cost(self, usd: float) -> None:
        with SessionLocal() as db:
            job = db.get(ProcessingJob, self.job_id)
            if job:
                job.cost_estimate_usd = (job.cost_estimate_usd or 0) + usd
                db.commit()


def finish(job_id: str, worker_id: str, status: str, error: str | None = None, result: dict | None = None) -> None:
    with SessionLocal() as db:
        job = db.get(ProcessingJob, job_id)
        if not job or job.worker_id != worker_id:
            return  # lease was lost to another worker; that worker owns the outcome now
        if job.status == "cancelled" or (job.cancel_requested and status != "succeeded"):
            status = "cancelled"
        now = utcnow()
        if status == "retry":
            if job.attempts < job.max_attempts:
                job.status = "queued"
                job.stage = f"Retrying after error (attempt {job.attempts} of {job.max_attempts})"
                job.error = error
                job.run_after = now + timedelta(seconds=min(300, 5 * 2 ** (job.attempts - 1)))
                job.lease_until = None
                db.commit()
                return
            status = "failed"
        job.status = status
        job.error = error
        job.lease_until = None
        job.finished_at = now
        if result is not None:
            job.result = result
        if status == "succeeded":
            job.progress = 1.0
            job.stage = "Done"
        elif status == "cancelled":
            job.stage = "Cancelled"
        elif status == "failed":
            job.stage = "Failed"
        db.commit()
