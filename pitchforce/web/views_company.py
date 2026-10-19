"""Company workspace: report preview/editing, questions, feedback, score, interest, video clip, transcript."""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import record
from ..auth import check_csrf, current_user, require_company, require_event
from ..config import get_settings
from ..db import get_db, utcnow
from ..jobs.queue import enqueue
from ..models import (
    AlignmentMap,
    AnnouncedTotal,
    Artifact,
    Claim,
    Clip,
    Company,
    CompanySegment,
    Feedback,
    JudgeAttendance,
    Person,
    PollResponse,
    ProcessingJob,
    Question,
    ReportSection,
    ReviewItem,
    Score,
    ShareLink,
    SourceFile,
    User,
    Utterance,
)
from ..reports import compose
from ..reports.xlsx import OUTREACH_STATUSES
from ..services import clips as clip_svc
from ..services import interest as interest_svc
from ..services.regenerate import mark_sections_stale, stale_artifacts
from ..services.scoring import compute_for_company, current_result
from ..storage import get_storage
from ..util import clean_text, parse_hms
from .common import flash, page

router = APIRouter()
TABS = ["report", "questions", "feedback", "score", "interest", "video", "transcript"]


def _base(db: Session, user: User, event_id: str, company_id: str):
    ev, c = require_company(db, user, event_id, company_id)
    return ev, c


@router.get("/events/{event_id}/companies/{company_id}")
def workspace(event_id: str, company_id: str, request: Request, tab: str = "report", db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    ev, c = _base(db, user, event_id, company_id)
    tab = tab if tab in TABS else "report"
    ctx = {"user": user, "event": ev, "company": c, "tab": tab, "tabs": TABS}
    ctx["presenters"] = list(db.scalars(select(Person).where(Person.company_id == c.id, Person.role == "presenter")))
    ctx["reviews"] = list(db.scalars(select(ReviewItem).where(ReviewItem.company_id == c.id, ReviewItem.status == "open")))
    ctx["segments"] = {s.kind: s for s in db.scalars(select(CompanySegment).where(CompanySegment.company_id == c.id))}
    ctx["running"] = db.scalar(select(ProcessingJob).where(ProcessingJob.company_id == c.id, ProcessingJob.status.in_(["queued", "running"])))
    if tab == "report":
        ctx["sections"] = compose.sections(db, c)
        ctx["words"] = compose.report_word_count(db, c)
        ctx["stale_artifacts"] = stale_artifacts(db, c)
    elif tab == "questions":
        qs = list(db.scalars(select(Question).where(Question.company_id == c.id).order_by(Question.seq, Question.part_label)))
        ctx["questions"] = [q for q in qs if q.parent_id is None]
        ctx["parts"] = {}
        for q in qs:
            if q.parent_id:
                ctx["parts"].setdefault(q.parent_id, []).append(q)
        ctx["claims"] = list(db.scalars(select(Claim).where(Claim.company_id == c.id)))
    elif tab == "feedback":
        fb = list(db.scalars(select(Feedback).where(Feedback.company_id == c.id).order_by(Feedback.seq)))
        by: dict[str, list[Feedback]] = {}
        for f in fb:
            by.setdefault(f.speaker_label, []).append(f)
        ctx["feedback_by"] = by
    elif tab == "score":
        ctx["result"] = current_result(db, c.id)
        ctx["scores"] = list(db.scalars(select(Score).where(Score.company_id == c.id)))
        ctx["unmatched_scores"] = list(db.scalars(select(Score).where(Score.event_id == ev.id, Score.company_id.is_(None))))
        ctx["announced"] = list(db.scalars(select(AnnouncedTotal).where(AnnouncedTotal.company_id == c.id)))
        ctx["judges"] = list(db.scalars(select(Person).where(Person.event_id == ev.id, Person.role == "judge").order_by(Person.display_name)))
        ctx["att"] = {a.person_id: a for a in db.scalars(select(JudgeAttendance).where(JudgeAttendance.event_id == ev.id))}
    elif tab == "interest":
        ctx["summary"] = interest_svc.summarize(db, ev, c)
        ctx["statuses"] = OUTREACH_STATUSES
        from ..models import INTEREST_LABELS

        ctx["labels"] = INTEREST_LABELS
    elif tab == "video":
        ctx["clip"] = db.scalar(select(Clip).where(Clip.company_id == c.id, Clip.variant == "core"))
        ctx["clip_score"] = db.scalar(select(Clip).where(Clip.company_id == c.id, Clip.variant == "with_score"))
        ctx["videos"] = clip_svc.videos(db, ev)
        ctx["alignment"] = db.scalar(select(AlignmentMap).where(AlignmentMap.event_id == ev.id)) if ctx["videos"] else None
        ctx["clip_art"] = None
        if ctx["clip"]:
            ctx["clip_art"] = next((a for a in db.scalars(select(Artifact).where(Artifact.company_id == c.id, Artifact.kind == "clip_mp4",
                                                                                 Artifact.is_current.is_(True)))
                                    if (a.meta or {}).get("variant", "core") == "core"), None)
        ctx["shares"] = list(db.scalars(select(ShareLink).where(ShareLink.company_id == c.id).order_by(ShareLink.created_at.desc())))
        # Transcript cues mapped to video time, for the clip editor's excerpt list
        cues = []
        if ctx["clip"] and ctx["alignment"] and "pitch" in ctx["segments"]:
            a = clip_svc.alignment_obj(ctx["alignment"])
            src_id = ctx["segments"]["pitch"].transcript_source_id
            lo = (ctx["clip"].start_media_s or 0) - 120
            hi = (ctx["clip"].end_media_s or 0) + 120
            for u in db.scalars(select(Utterance).where(Utterance.source_file_id == src_id).order_by(Utterance.seq)):
                m, q = a.to_media(u.start_s)
                if m is not None and lo <= m <= hi:
                    cues.append({"t": round(m, 2), "raw": u.start_raw, "speaker": u.speaker, "text": u.text[:220], "q": q,
                                 "company": u.company_id == c.id})
        ctx["cues"] = cues
    elif tab == "transcript":
        segs = ctx["segments"]
        src_id = next((s.transcript_source_id for s in segs.values() if s.transcript_source_id), None)
        rows = []
        if src_id:
            all_u = list(db.scalars(select(Utterance).where(Utterance.source_file_id == src_id).order_by(Utterance.seq)))
            idx = [i for i, u in enumerate(all_u) if u.company_id == c.id]
            if idx:
                rows = all_u[max(0, idx[0] - 8): idx[-1] + 9]
        ctx["utts"] = rows
        ctx["companies"] = list(db.scalars(select(Company).where(Company.event_id == ev.id).order_by(Company.slot_order)))
        ctx["seg_by_start"] = {s.start_utt_id: s for s in segs.values()}
        ctx["seg_by_end"] = {s.end_utt_id: s for s in segs.values()}
    return page(request, db, "company.html", ctx)


def _back(ev, c, tab):
    return RedirectResponse(f"/events/{ev.id}/companies/{c.id}?tab={tab}", status_code=303)


@router.post("/events/{event_id}/companies/{company_id}/sections/{key}", dependencies=[Depends(check_csrf)])
async def section_action(event_id: str, company_id: str, key: str, request: Request, db: Session = Depends(get_db),
                         user: User = Depends(current_user)):
    ev, c = _base(db, user, event_id, company_id)
    sec = db.scalar(select(ReportSection).where(ReportSection.company_id == c.id, ReportSection.key == key))
    if not sec:
        raise HTTPException(404, "Section not found")
    f = await request.form()
    a = f.get("action")
    before = {"text": sec.text[:2000], "approved": sec.approved}
    if a == "save":
        text = clean_text(f.get("text", ""), 20000).replace("\r\n", "\n")
        sec.edited_text = text if text != sec.generated_text else None
        sec.edited_against_hash = sec.input_hash
        sec.stale, sec.stale_reason = False, ""
        flash(request, f"“{sec.title}” saved. Your edit is kept when other sections are regenerated.", "success")
    elif a == "approve":
        sec.approved, sec.stale, sec.stale_reason = True, False, ""
        flash(request, f"“{sec.title}” approved.", "success")
    elif a == "unapprove":
        sec.approved = False
    elif a == "revert":
        sec.edited_text, sec.approved, sec.stale, sec.stale_reason = None, False, False, ""
        flash(request, f"“{sec.title}” reverted to the generated text.", "info")
    elif a == "regenerate":
        compose.compose(db, ev, c, keys=[key], force=True, discard_edits=f.get("discard") == "on", user_id=user.id)
        flash(request, f"“{sec.title}” regenerated" + (" (your edit was discarded)." if f.get("discard") == "on" else "."), "info")
    sec.updated_by = user.id
    record(db, f"section:{a}", "report_section", sec.id, ev.id, before=before, after={"text": sec.text[:2000], "approved": sec.approved},
           user_id=user.id)
    db.commit()
    return RedirectResponse(f"/events/{ev.id}/companies/{c.id}?tab=report#sec-{key}", status_code=303)


@router.post("/events/{event_id}/companies/{company_id}/action", dependencies=[Depends(check_csrf)])
async def company_action(event_id: str, company_id: str, request: Request, db: Session = Depends(get_db),
                         user: User = Depends(current_user)):
    ev, c = _base(db, user, event_id, company_id)
    f = await request.form()
    a = f.get("action")
    tab = f.get("tab") or "report"
    if a == "regenerate":
        enqueue(db, "process_company", c.id, {"company_id": c.id}, event_id=ev.id, company_id=c.id, user_id=user.id, rerun=True)
        flash(request, "Regenerating this company's analysis and report. Approved or edited sections are kept.", "info")
    elif a == "approve":
        c.status = "approved"
        flash(request, f"{c.display_name} marked as reviewed and approved.", "success")
    elif a == "question":
        q = db.get(Question, f.get("question_id"))
        if not q or q.company_id != c.id:
            raise HTTPException(404, "Question not found")
        if f.get("op") == "reject":
            q.status = "rejected"
        elif f.get("op") == "confirm":
            q.status = "confirmed"
        else:
            q.paraphrase = clean_text(f.get("paraphrase", q.paraphrase), 1000)
            q.unresolved = clean_text(f.get("unresolved", q.unresolved), 1000)
            q.asker_role = f.get("asker_role") if f.get("asker_role") in ("investor", "moderator", "audience", "unknown") else q.asker_role
            q.status = "edited"
        mark_sections_stale(db, c, ["questions", "assessment", "priorities"], "Questions were edited; regenerate or review.")
        flash(request, "Question updated.", "success")
    elif a == "feedback":
        fb = db.get(Feedback, f.get("feedback_id"))
        if not fb or fb.company_id != c.id:
            raise HTTPException(404, "Feedback not found")
        if f.get("op") == "reject":
            fb.status = "rejected"
        else:
            fb.point = clean_text(f.get("point", fb.point), 1000)
            if f.get("kind") in ("positive", "recommendation", "comment", "nothing_to_add", "silent"):
                fb.kind = f.get("kind")
            fb.theme = f.get("theme") or fb.theme
            fb.status = "edited"
        mark_sections_stale(db, c, ["worked", "priorities", "assessment"], "Feedback was edited; regenerate or review.")
        flash(request, "Feedback updated.", "success")
    elif a == "claim":
        cl = db.get(Claim, f.get("claim_id"))
        if not cl or cl.company_id != c.id:
            raise HTTPException(404, "Claim not found")
        cl.verified = f.get("op") == "verify"
        cl.verified_by = user.id if cl.verified else None
        flash(request, "Claim marked as operator-verified." if cl.verified else "Claim marked as unverified.", "success")
    elif a == "announced":
        op = f.get("op")
        if op == "add":
            raw = (f.get("value") or "").strip()
            try:
                val = float(raw)
            except ValueError:
                flash(request, "Enter a number for the announced total.", "error")
                return _back(ev, c, "score")
            db.add(AnnouncedTotal(company_id=c.id, value_raw=raw, value=val, denominator=ev.expected_judge_count * ev.score_max,
                                  source_kind="manual", excerpt=clean_text(f.get("note", ""), 500), status="confirmed"))
        else:
            at = db.get(AnnouncedTotal, f.get("announced_id"))
            if at and at.company_id == c.id:
                at.status = "confirmed" if op == "confirm" else "rejected"
        db.flush()
        compute_for_company(db, ev, c)
        enqueue(db, "process_company", c.id, {"company_id": c.id}, event_id=ev.id, company_id=c.id, user_id=user.id, rerun=True)
        flash(request, "Announced total updated.", "success")
    elif a == "score_status":
        s = db.get(Score, f.get("score_id"))
        if not s or s.event_id != ev.id:
            raise HTTPException(404, "Score not found")
        s.status = "rejected" if f.get("op") == "reject" else s.status
        s.status_reason = clean_text(f.get("note", ""), 500) or s.status_reason
        compute_for_company(db, ev, c)
        flash(request, "Score updated.", "success")
    elif a == "outreach":
        for k, v in f.items():
            if k.startswith("status_"):
                r = db.get(PollResponse, k[7:])
                if r and r.company_id == c.id and v in OUTREACH_STATUSES:
                    r.outreach_status = v
            if k.startswith("note_"):
                r = db.get(PollResponse, k[5:])
                if r and r.company_id == c.id:
                    r.outreach_note = clean_text(v, 500)
        flash(request, "Outreach status saved (included in the next workbook you generate).", "success")
    elif a == "assign_utterances":
        target = db.get(Company, f.get("target_company_id")) if f.get("target_company_id") else None
        ids = f.getlist("utterance_id")
        n = 0
        for uid in ids:
            u = db.get(Utterance, uid)
            if u and u.event_id == ev.id:
                u.company_id = target.id if target and target.event_id == ev.id else None
                u.assignment_source = "operator"
                n += 1
        flash(request, f"{n} transcript line(s) reassigned. Re-run processing to rebuild boundaries and reports.", "success")
    elif a == "speaker_override":
        u = db.get(Utterance, f.get("utterance_id"))
        if u and u.event_id == ev.id:
            u.speaker_override = clean_text(f.get("speaker", ""), 300) or None
            flash(request, "Speaker corrected for that line. Re-run processing to apply it.", "success")
    elif a == "segment":
        seg = db.get(CompanySegment, f.get("segment_id"))
        if not seg or seg.company_id != c.id:
            raise HTTPException(404, "Segment not found")
        if f.get("op") == "confirm":
            seg.status, seg.updated_by = "confirmed", user.id
        flash(request, f"{seg.kind.replace('_', ' ').title()} boundary confirmed.", "success")
    else:
        raise HTTPException(400, "Unknown action")
    record(db, f"company:{a}", "company", c.id, ev.id, after={k: v for k, v in f.items() if k != "csrf_token" and not k.startswith("note_")},
           user_id=user.id)
    db.commit()
    return _back(ev, c, tab)


# ---------------------------------------------------------------------------
# Clip editing and rendering
# ---------------------------------------------------------------------------
@router.post("/events/{event_id}/companies/{company_id}/clip", dependencies=[Depends(check_csrf)])
async def clip_action(event_id: str, company_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev, c = _base(db, user, event_id, company_id)
    f = await request.form()
    variant = "with_score" if f.get("variant") == "with_score" else "core"
    clip = db.scalar(select(Clip).where(Clip.company_id == c.id, Clip.variant == variant))
    if clip is None:
        clip = clip_svc.update_candidate(db, ev, c, variant)
    a = f.get("action")
    if a == "boundaries":
        def secs(v):
            v = (v or "").strip()
            return parse_hms(v) if ":" in v else (float(v) if v else None)

        try:
            start, end = secs(f.get("start")), secs(f.get("end"))
            if start is None or end is None:
                raise ValueError("Enter both a start and an end time.")
            clip_svc.set_operator_boundaries(db, clip, start, end, user.id)
            flash(request, "Clip boundaries saved. Outputs that used the old boundaries are marked stale.", "success")
        except ValueError as e:
            flash(request, str(e), "error")
    elif a == "reset":
        clip.boundary_source = "auto"
        clip_svc.update_candidate(db, ev, c, variant)
        flash(request, "Clip boundaries reset to the detected pitch-through-feedback range.", "info")
    elif a == "render":
        if clip.status in ("boundary_candidate", "alignment_review") and f.get("confirm") != "on":
            flash(request, "Check the boundaries in the player, tick “Boundaries checked”, then render.", "error")
        elif clip.start_media_s is None:
            flash(request, "This clip has no boundaries yet.", "error")
        else:
            if clip.status in ("boundary_candidate", "alignment_review"):
                clip.status = "ready_to_render"
            enqueue(db, "render_clip", f"{clip.id}:{clip.boundary_hash}", {"clip_id": clip.id, "boundary_hash": clip.boundary_hash},
                    event_id=ev.id, company_id=c.id, user_id=user.id, rerun=clip.status == "failed")
            flash(request, "Rendering started in the background.", "info")
    elif a == "reviewed":
        if clip.status == "rendered":
            clip.status = "reviewed"
            flash(request, "Clip marked as reviewed.", "success")
    elif a == "share":
        art = next((x for x in db.scalars(select(Artifact).where(Artifact.company_id == c.id, Artifact.kind == "clip_mp4",
                                                                 Artifact.is_current.is_(True)))
                    if (x.meta or {}).get("variant", "core") == variant), None)
        if not art or clip.status not in ("rendered", "reviewed", "shared"):
            flash(request, "Render and review the clip before creating a share link.", "error")
        else:
            token = secrets.token_urlsafe(24)
            days = max(1, min(90, int(f.get("days") or get_settings().share_link_days)))
            db.add(ShareLink(event_id=ev.id, company_id=c.id, artifact_id=art.id, token_hash=hashlib.sha256(token.encode()).hexdigest(),
                             expires_at=utcnow() + timedelta(days=days), created_by=user.id))
            clip.share_url = f"{get_settings().base_url.rstrip('/')}/s/{token}"
            clip.status = "shared"
            mark_sections_stale(db, c, ["replay"], "A share link was created.")
            compose.compose(db, ev, c, keys=["replay"])
            flash(request, f"Share link created (expires in {days} days). It works only while this server is reachable at "
                           f"{get_settings().base_url}.", "success")
    elif a == "revoke":
        sl = db.get(ShareLink, f.get("share_id"))
        if sl and sl.company_id == c.id:
            sl.revoked = True
            if clip.share_url:
                clip.share_url = None
                clip.status = "reviewed" if clip.rendered_hash else clip.status
                compose.compose(db, ev, c, keys=["replay"])
            flash(request, "Share link revoked.", "info")
    record(db, f"clip:{a}", "clip", clip.id, ev.id, after={"start": clip.start_media_s, "end": clip.end_media_s, "status": clip.status},
           user_id=user.id)
    db.commit()
    return _back(ev, c, "video")


# ---------------------------------------------------------------------------
# Media streaming (authorized, Range-capable) and public share links
# ---------------------------------------------------------------------------
def _range_response(path, request: Request, media_type: str, filename: str | None = None):
    size = path.stat().st_size
    rng = request.headers.get("range")
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-store"}
    if filename:
        headers["Content-Disposition"] = f'inline; filename="{filename}"'
    if rng and rng.startswith("bytes="):
        try:
            s, e = rng[6:].split(",")[0].split("-")
            start = int(s) if s else max(0, size - int(e))
            end = int(e) if (e and s) else size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                raise ValueError
        except ValueError:
            return StreamingResponse(iter([b""]), status_code=416, headers={"Content-Range": f"bytes */{size}"})
        st = get_storage()
        key = path.relative_to(st.root).as_posix()
        headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(end - start + 1)})
        return StreamingResponse(st.iter_range(key, start, end), status_code=206, media_type=media_type, headers=headers)
    return FileResponse(path, media_type=media_type, headers=headers)


@router.get("/events/{event_id}/media/{source_id}")
def stream_source(event_id: str, source_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = require_event(db, user, event_id)
    src = db.get(SourceFile, source_id)
    if not src or src.event_id != ev.id or src.kind != "video":
        raise HTTPException(404, "Video not found")
    return _range_response(get_storage().path(src.storage_key), request, "video/mp4")


@router.get("/events/{event_id}/companies/{company_id}/clipfile")
def stream_clip(event_id: str, company_id: str, request: Request, variant: str = "core", db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    ev, c = _base(db, user, event_id, company_id)
    clip = db.scalar(select(Clip).where(Clip.company_id == c.id, Clip.variant == variant))
    if not clip or not clip.storage_key:
        raise HTTPException(404, "Clip not rendered")
    return _range_response(get_storage().path(clip.storage_key), request, "video/mp4")


@router.get("/events/{event_id}/companies/{company_id}/thumb/{label}")
def clip_thumb(event_id: str, company_id: str, label: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev, c = _base(db, user, event_id, company_id)
    clip = db.scalar(select(Clip).where(Clip.company_id == c.id, Clip.variant == "core"))
    key = ((clip.validation or {}).get("thumbnails") or {}).get(label) if clip else None
    if not key or not get_storage().exists(key):
        raise HTTPException(404, "No thumbnail")
    return FileResponse(get_storage().path(key), media_type="image/jpeg")


@router.get("/s/{token}")
def shared(token: str, request: Request, db: Session = Depends(get_db)):
    """Public, expiring, revocable link to one founder artifact (e.g. a clip). Exposes nothing else."""
    h = hashlib.sha256(token.encode()).hexdigest()
    sl = db.scalar(select(ShareLink).where(ShareLink.token_hash == h))
    if not sl or sl.revoked or sl.expires_at < utcnow():
        raise HTTPException(404, "This link has expired or is no longer available.")
    a = db.get(Artifact, sl.artifact_id)
    if not a or a.audience != "founder" or a.company_id != sl.company_id:
        raise HTTPException(404, "This link has expired or is no longer available.")
    sl.last_used_at = utcnow()
    db.commit()
    return _range_response(get_storage().path(a.storage_key), request,
                           "video/mp4" if a.kind == "clip_mp4" else "application/octet-stream", a.filename)


_ = (require_event,)
