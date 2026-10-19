"""Background job handlers. Each handler is idempotent and reports progress by real stage."""
from __future__ import annotations

from sqlalchemy import select

from ..db import SessionLocal, session_scope
from ..models import AIUsage, Clip, Company, Event, ProcessingJob, SourceFile
from .queue import JobContext, PermanentJobError, enqueue, handler


@handler("ingest_source")
def ingest_source_job(ctx: JobContext) -> dict:
    from ..media.ffmpeg import MediaError
    from ..parsing.documents import ArchiveError
    from ..services.ingest import ingest_source

    sid = ctx.payload["source_id"]
    with SessionLocal() as db:
        src = db.get(SourceFile, sid)
        if not src:
            raise PermanentJobError("Source file no longer exists.")
        ctx.progress(f"Reading {src.original_filename}", 0.2)
        try:
            summary = ingest_source(db, src)
            db.commit()
            return summary
        except (MediaError, ArchiveError, ValueError, KeyError, IndexError) as e:
            db.rollback()
            src = db.get(SourceFile, sid)
            src.status = "failed"
            src.parse_error = f"{type(e).__name__}: {e}" if not isinstance(e, (MediaError, ArchiveError, ValueError)) else str(e)
            db.commit()
            raise PermanentJobError(f"Could not read {src.original_filename}: {src.parse_error}") from e


@handler("analyze_event")
def analyze_event_job(ctx: JobContext) -> dict:
    from ..analysis.pipeline import analyze_event

    with SessionLocal() as db:
        event = db.get(Event, ctx.event_id)
        res = analyze_event(db, event, progress=ctx.progress)
        event.status = "processing"
        db.commit()
        companies = list(db.scalars(select(Company).where(Company.event_id == event.id)))
        ctx.progress("Starting per-company analysis", 0.9)
        for c in companies:
            c.status = "processing"
            enqueue(db, "process_company", c.id, {"company_id": c.id}, event_id=event.id, company_id=c.id,
                    parent_id=ctx.job_id, rerun=True)
        db.commit()
        return res


@handler("process_company")
def process_company_job(ctx: JobContext) -> dict:
    from ..services.regenerate import process_company

    with SessionLocal() as db:
        company = db.get(Company, ctx.payload["company_id"])
        event = db.get(Event, company.event_id)
        try:
            res = process_company(db, event, company, progress=ctx.progress)
            spent = sum(u.est_cost_usd for u in db.scalars(select(AIUsage).where(AIUsage.company_id == company.id, AIUsage.job_id.is_(None))))
            for u in db.scalars(select(AIUsage).where(AIUsage.company_id == company.id, AIUsage.job_id.is_(None))):
                u.job_id = ctx.job_id
            db.commit()
            if spent:
                ctx.add_cost(spent)
            _maybe_finish_event(event.id)
            return {k: v for k, v in res.items() if k != "input_hash"}
        except Exception as e:
            db.rollback()
            with session_scope() as db2:
                c = db2.get(Company, company.id)
                c.status, c.last_error = "failed", f"{type(e).__name__}: {e}"
            raise


def _maybe_finish_event(event_id: str) -> None:
    with session_scope() as db:
        ev = db.get(Event, event_id)
        statuses = [c.status for c in db.scalars(select(Company).where(Company.event_id == event_id))]
        if statuses and all(s in ("ready", "failed", "approved") for s in statuses):
            ev.status = "review"


@handler("render_clip")
def render_clip_job(ctx: JobContext) -> dict:
    from ..services.clips import render

    with SessionLocal() as db:
        clip = db.get(Clip, ctx.payload["clip_id"])
        if clip.boundary_hash != ctx.payload.get("boundary_hash"):
            raise PermanentJobError("Clip boundaries changed after this render was requested; request a new render.")
        try:
            art = render(db, clip, progress=ctx.progress, should_cancel=lambda: _cancel_requested(ctx.job_id))
        except Exception as e:
            db.rollback()
            with session_scope() as db2:
                c = db2.get(Clip, clip.id)
                c.status, c.status_reason = "failed", str(e)
            if "cancelled" in str(e):
                from .queue import JobCancelled

                raise JobCancelled() from e
            raise
        db.commit()
        return {"artifact": art.id, "duration_s": clip.duration_s, "ok": clip.validation.get("ok")}


def _cancel_requested(job_id: str) -> bool:
    with SessionLocal() as db:
        j = db.get(ProcessingJob, job_id)
        return bool(j and j.cancel_requested)


@handler("build_company_package")
def build_company_package_job(ctx: JobContext) -> dict:
    from ..reports.package import build_company_package

    with SessionLocal() as db:
        company = db.get(Company, ctx.payload["company_id"])
        event = db.get(Event, company.event_id)
        res = build_company_package(db, event, company, include_clip=ctx.payload.get("include_clip", True),
                                    include_evidence=ctx.payload.get("include_evidence", True), job_id=ctx.job_id, progress=ctx.progress)
        db.commit()
        return res


@handler("build_event_package")
def build_event_package_job(ctx: JobContext) -> dict:
    from ..reports.package import build_event_package

    with SessionLocal() as db:
        event = db.get(Event, ctx.event_id)
        res = build_event_package(db, event, include_clips=ctx.payload.get("include_clips", True), job_id=ctx.job_id, progress=ctx.progress)
        db.commit()
        return res


@handler("calendar_refresh")
def calendar_refresh_job(ctx: JobContext) -> dict:
    from ..services import calendar

    with SessionLocal() as db:
        ctx.progress("Retrieving the official EcoVG calendar", 0.2)
        res = calendar.refresh(db, force=True)
        db.commit()
        return res


@handler("transcribe")
def transcribe_job(ctx: JobContext) -> dict:
    from ..config import get_settings
    from ..media.transcription import TranscriptionError, estimate_cost, transcribe
    from ..services.ingest import ingest_source
    from ..services.sources import register_bytes
    from ..storage import get_storage

    if not get_settings().transcription_configured:
        raise PermanentJobError("No transcription provider is configured.")
    with SessionLocal() as db:
        video = db.get(SourceFile, ctx.payload["source_id"])
        event = db.get(Event, video.event_id)
        dur = (video.meta or {}).get("probe", {}).get("duration_s") or 0
        try:
            vtt = transcribe(get_storage().path(video.storage_key), dur, progress=ctx.progress)
        except TranscriptionError as e:
            raise PermanentJobError(str(e)) from e
        src = register_bytes(db, event, "transcript", video.original_filename.rsplit(".", 1)[0] + "_transcribed.vtt", vtt.encode("utf-8"))
        src.meta = {**(src.meta or {}), "generated_by": "transcription", "from_video": video.id}
        ingest_source(db, src)
        db.add(AIUsage(event_id=event.id, provider=get_settings().transcription_provider, model=get_settings().transcription_model,
                       purpose="transcription", audio_minutes=dur / 60, est_cost_usd=estimate_cost(dur), job_id=ctx.job_id))
        db.commit()
        ctx.add_cost(estimate_cost(dur))
        return {"transcript_source_id": src.id}
