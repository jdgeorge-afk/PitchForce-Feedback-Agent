"""JSON API used by the browser: resumable chunked uploads and job progress/control."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import record
from ..auth import check_csrf, current_user, require_event
from ..config import get_settings
from ..db import get_db
from ..jobs import queue
from ..models import Company, ProcessingJob, SourceFile, Upload, User
from ..services.processing import queue_ingest
from ..services.sources import SourceValidationError, register_file, validate_content, validate_name_and_size
from ..storage import get_storage
from .common import flash

router = APIRouter()


class UploadInit(BaseModel):
    kind: str
    filename: str
    size: int


@router.post("/api/events/{event_id}/uploads", dependencies=[Depends(check_csrf)])
def upload_init(event_id: str, body: UploadInit, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    try:
        validate_name_and_size(body.kind, body.filename, body.size)
    except SourceValidationError as e:
        raise HTTPException(400, str(e)) from e
    # Resume: same user, file name, size and kind with an unfinished upload.
    existing = db.scalar(select(Upload).where(Upload.event_id == ev.id, Upload.created_by == user.id, Upload.filename == body.filename,
                                              Upload.total_size == body.size, Upload.kind == body.kind, Upload.status == "receiving"))
    if existing:
        tmp = get_storage().upload_temp_path(existing.id)
        existing.received = tmp.stat().st_size if tmp.exists() else 0
        db.commit()
        return {"upload_id": existing.id, "received": existing.received, "chunk_size": get_settings().upload_chunk_mb * 1024 * 1024}
    up = Upload(event_id=ev.id, kind=body.kind, filename=Path(body.filename).name[:500], total_size=body.size, created_by=user.id)
    db.add(up)
    db.commit()
    return {"upload_id": up.id, "received": 0, "chunk_size": get_settings().upload_chunk_mb * 1024 * 1024}


def _upload_for(db: Session, user: User, upload_id: str) -> Upload:
    up = db.get(Upload, upload_id)
    if not up or up.created_by != user.id:
        raise HTTPException(404, "Upload not found")
    require_event(db, user, up.event_id)
    return up


@router.get("/api/uploads/{upload_id}")
def upload_status(upload_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    up = _upload_for(db, user, upload_id)
    tmp = get_storage().upload_temp_path(up.id)
    return {"received": tmp.stat().st_size if tmp.exists() else 0, "total": up.total_size, "status": up.status}


@router.put("/api/uploads/{upload_id}", dependencies=[Depends(check_csrf)])
async def upload_chunk(upload_id: str, request: Request, offset: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    up = _upload_for(db, user, upload_id)
    if up.status != "receiving":
        raise HTTPException(409, "Upload is not accepting data")
    tmp = get_storage().upload_temp_path(up.id)
    have = tmp.stat().st_size if tmp.exists() else 0
    if offset != have:
        return JSONResponse({"error": "offset mismatch", "received": have}, status_code=409)
    limit = get_settings().upload_chunk_mb * 1024 * 1024 + 1024
    written = 0
    with open(tmp, "ab") as fh:
        async for chunk in request.stream():
            written += len(chunk)
            if written > limit or have + written > up.total_size:
                fh.truncate(have)
                raise HTTPException(413, "Chunk too large")
            fh.write(chunk)
    if have == 0:
        with open(tmp, "rb") as fh:
            head = fh.read(8192)
        try:
            validate_content(up.kind, up.filename, head)
        except SourceValidationError as e:
            tmp.unlink(missing_ok=True)
            up.status, up.error = "failed", str(e)
            db.commit()
            raise HTTPException(400, str(e)) from e
    up.received = have + written
    db.commit()
    return {"received": up.received}


@router.post("/api/uploads/{upload_id}/complete", dependencies=[Depends(check_csrf)])
def upload_complete(upload_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    up = _upload_for(db, user, upload_id)
    ev = require_event(db, user, up.event_id)
    tmp = get_storage().upload_temp_path(up.id)
    have = tmp.stat().st_size if tmp.exists() else 0
    if have != up.total_size:
        raise HTTPException(400, f"Upload incomplete ({have} of {up.total_size} bytes)")
    try:
        src = register_file(db, ev, up.kind, up.filename, tmp, user.id, move=True)
    except SourceValidationError as e:
        up.status, up.error = "failed", str(e)
        db.commit()
        raise HTTPException(400, str(e)) from e
    up.status, up.source_file_id = "complete", src.id
    job = queue_ingest(db, ev, src, user.id)
    record(db, "upload", "source_file", src.id, ev.id, after={"name": src.original_filename, "kind": src.kind, "sha256": src.sha256},
           user_id=user.id)
    db.commit()
    return {"source_id": src.id, "job_id": job.id, "duplicate": src.created_at < up.created_at}


@router.delete("/api/uploads/{upload_id}", dependencies=[Depends(check_csrf)])
def upload_abort(upload_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    up = _upload_for(db, user, upload_id)
    get_storage().upload_temp_path(up.id).unlink(missing_ok=True)
    up.status = "aborted"
    db.commit()
    return {"ok": True}


@router.get("/api/events/{event_id}/jobs")
def jobs_json(event_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    jobs = list(db.scalars(select(ProcessingJob).where(ProcessingJob.event_id == ev.id).order_by(ProcessingJob.created_at.desc()).limit(60)))
    names = {c.id: c.display_name for c in db.scalars(select(Company).where(Company.event_id == ev.id))}
    srcs = {s.id: s.original_filename for s in db.scalars(select(SourceFile).where(SourceFile.event_id == ev.id))}
    out = []
    for j in jobs:
        label = j.kind.replace("_", " ")
        if j.company_id in names:
            label += f" – {names[j.company_id]}"
        if j.payload.get("source_id") in srcs:
            label += f" – {srcs[j.payload['source_id']]}"
        out.append({"id": j.id, "kind": j.kind, "label": label, "status": j.status, "stage": j.stage, "progress": round(j.progress, 3),
                    "error": j.error, "attempts": j.attempts, "max_attempts": j.max_attempts, "cost": round(j.cost_estimate_usd or 0, 4)})
    active = any(j["status"] in ("queued", "running") for j in out)
    return {"jobs": out, "active": active, "event_status": ev.status}


@router.post("/jobs/{job_id}/{action}", dependencies=[Depends(check_csrf)])
def job_action(job_id: str, action: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    j = db.get(ProcessingJob, job_id)
    if not j or not j.event_id:
        raise HTTPException(404, "Job not found")
    ev = require_event(db, user, j.event_id)
    if action == "retry":
        queue.retry(db, j)
        flash(request, "Job queued again.", "info")
    elif action == "cancel":
        queue.cancel(db, j)
        flash(request, "Cancellation requested.", "info")
    else:
        raise HTTPException(400, "Unknown action")
    record(db, f"job:{action}", "job", j.id, ev.id, user_id=user.id)
    db.commit()
    back = request.headers.get("referer") or f"/events/{ev.id}/jobs"
    from urllib.parse import urlparse

    path = urlparse(back).path or f"/events/{ev.id}/jobs"
    return RedirectResponse(path if path.startswith("/events/") else f"/events/{ev.id}/jobs", status_code=303)


_ = os
