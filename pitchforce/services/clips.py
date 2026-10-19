"""Clip boundaries, transcript/video alignment, rendering, and validation.

Default clip scope: start of the actual pitch (or the founder's earlier substantive opening update)
through the end of the final panel feedback. The score announcement is a separate, optional variant.
Durations come from the real content - nothing is padded or trimmed to hit a target length.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..media import alignment as AL
from ..media import ffmpeg
from ..models import AlignmentMap, Artifact, Clip, Company, CompanySegment, Event, SourceFile, Utterance
from ..storage import get_storage
from ..util import fmt_duration, fmt_hms, stable_hash
from . import matching


def videos(db: Session, event: Event) -> list[SourceFile]:
    return list(db.scalars(select(SourceFile).where(SourceFile.event_id == event.id, SourceFile.kind == "video",
                                                    SourceFile.status == "parsed")))


def _uv(db: Session, src: SourceFile) -> list[AL.U]:
    return [AL.U(u.start_s, u.text, u.speaker, u.cue_ref)
            for u in db.scalars(select(Utterance).where(Utterance.source_file_id == src.id).order_by(Utterance.seq))]


def build_alignment(db: Session, event: Event, video: SourceFile, transcript: SourceFile) -> AlignmentMap:
    am = db.scalar(select(AlignmentMap).where(AlignmentMap.event_id == event.id, AlignmentMap.video_source_id == video.id,
                                              AlignmentMap.transcript_source_id == transcript.id))
    if am and am.status == "confirmed":
        return am
    duration = (video.meta or {}).get("probe", {}).get("duration_s")
    prim = _uv(db, transcript)
    if transcript.time_basis == "media":
        a = AL.identity(duration, prim)
    else:
        refs = [s for s in db.scalars(select(SourceFile).where(SourceFile.event_id == event.id, SourceFile.kind == "transcript",
                                                               SourceFile.status == "parsed", SourceFile.time_basis == "media"))]
        a = None
        for ref in refs:
            cand = AL.anchor_match(prim, _uv(db, ref))
            if cand.status != "failed":
                cand.notes.insert(0, f"Aligned by matching passages with the recording-relative transcript “{ref.original_filename}”.")
                a = cand
                break
        if a is None and event.recording_start_wallclock:
            a = AL.operator_offset(event.recording_start_wallclock, prim)
        if a is None:
            a = AL.Alignment(method="none", status="failed",
                             notes=["No way to map this wall-clock transcript to video time: upload the recording's own VTT/SRT "
                                    "transcript, or enter the time the recording started (event settings)."])
    am = am or AlignmentMap(event_id=event.id, video_source_id=video.id, transcript_source_id=transcript.id)
    am.method = a.method
    am.pieces = [p.__dict__ for p in a.pieces]
    am.anchors = a.anchors[:200]
    am.residual_max_s = a.residual_max_s
    am.drift_detected = a.drift_detected
    am.status = a.status
    am.notes = "\n".join(a.notes)
    db.add(am)
    db.flush()
    if a.status in ("needs_review", "failed"):
        matching.open_review(db, event.id, "alignment", f"align:{video.id}:{transcript.id}:{a.method}",
                             f"Transcript-to-video alignment {'failed' if a.status == 'failed' else 'needs review'}",
                             " ".join(a.notes), severity="high" if a.status == "failed" else "medium",
                             evidence={"method": a.method, "residual": a.residual_max_s}, target_type="alignment", target_id=am.id)
    return am


def alignment_obj(am: AlignmentMap) -> AL.Alignment:
    return AL.Alignment.from_json({"method": am.method, "pieces": am.pieces, "anchors": am.anchors,
                                   "residual_max_s": am.residual_max_s, "drift_detected": am.drift_detected,
                                   "status": am.status, "notes": (am.notes or "").split("\n")})


def _segs(db: Session, company: Company) -> dict[str, CompanySegment]:
    return {s.kind: s for s in db.scalars(select(CompanySegment).where(CompanySegment.company_id == company.id))}


def transcript_scope(db: Session, company: Company, variant: str = "core") -> tuple[float | None, float | None, list[str]]:
    segs = _segs(db, company)
    notes = []
    starts = [segs[k].start_s for k in ("opening_update", "pitch") if k in segs and segs[k].start_s is not None]
    start = min(starts) if starts else None
    end_kind = "feedback" if "feedback" in segs else ("qa" if "qa" in segs else "pitch")
    if end_kind != "feedback":
        notes.append(f"No feedback section found; clip ends at the end of the {end_kind}.")
    end = segs[end_kind].end_s if end_kind in segs else None
    if variant == "with_score" and "score" in segs and segs["score"].end_s is not None:
        end = max(end or 0, segs["score"].end_s)
    return start, end, notes


def update_candidate(db: Session, event: Event, company: Company, variant: str = "core") -> Clip:
    clip = db.scalar(select(Clip).where(Clip.company_id == company.id, Clip.variant == variant))
    clip = clip or Clip(company_id=company.id, variant=variant)
    db.add(clip)
    vids = videos(db, event)
    if not vids:
        clip.status, clip.status_reason = "unavailable", "No video recording was uploaded for this event."
        db.flush()
        return clip
    video = vids[0]
    segs = _segs(db, company)
    if "pitch" not in segs:
        clip.status, clip.status_reason = "unavailable", "This company's pitch was not located in the transcript."
        db.flush()
        return clip
    transcript = db.get(SourceFile, segs["pitch"].transcript_source_id)
    am = build_alignment(db, event, video, transcript)
    clip.video_source_id = video.id
    if clip.boundary_source == "operator" and clip.start_media_s is not None:
        pass  # keep operator-adjusted boundaries
    else:
        t0, t1, notes = transcript_scope(db, company, variant)
        a = alignment_obj(am)
        m0, q0 = a.to_media(t0)
        m1, q1 = a.to_media(t1)
        clip.start_media_s, clip.end_media_s = m0, m1
        if am.status == "failed" or m0 is None or m1 is None:
            clip.status, clip.status_reason = "alignment_review", "Video time for this company could not be determined. " + (am.notes or "")
            db.flush()
            return clip
        reasons = list(notes)
        if q0 != "mapped" or q1 != "mapped":
            reasons.append(f"Boundary falls outside the aligned range ({q0}/{q1}); check it in the player.")
        dur = (video.meta or {}).get("probe", {}).get("duration_s")
        if dur is not None:
            if m0 < 0:
                reasons.append(f"Pitch starts {fmt_hms(-m0)} before the recording begins; the start is clamped to 0:00.")
                clip.start_media_s = 0.0
            if m1 > dur:
                reasons.append("Feedback ends after the recording ends; clamped to the end of the video.")
                clip.end_media_s = dur
        clip.status_reason = " ".join(reasons)
    needs_review = am.status not in ("ok", "confirmed") or any(
        segs[k].status == "needs_review" for k in ("opening_update", "pitch", "feedback") if k in segs)
    new_hash = stable_hash([clip.video_source_id, round(clip.start_media_s or 0, 2), round(clip.end_media_s or 0, 2)])
    if clip.rendered_hash and clip.rendered_hash != new_hash:
        mark_clip_artifacts_stale(db, company, "Clip boundaries changed after rendering.")
    clip.boundary_hash = new_hash
    if clip.status in ("rendered", "reviewed", "shared") and clip.rendered_hash == new_hash:
        pass
    elif clip.status == "rendering":
        pass
    else:
        clip.status = "alignment_review" if am.status in ("needs_review", "failed") else (
            "boundary_candidate" if needs_review and clip.boundary_source != "operator" else "ready_to_render")
    db.flush()
    return clip


def set_operator_boundaries(db: Session, clip: Clip, start: float, end: float, user_id: str | None) -> None:
    if end <= start:
        raise ValueError("The end must be after the start.")
    clip.start_media_s, clip.end_media_s = round(start, 3), round(end, 3)
    clip.boundary_source = "operator"
    new_hash = stable_hash([clip.video_source_id, round(start, 2), round(end, 2)])
    company = db.get(Company, clip.company_id)
    if clip.rendered_hash and clip.rendered_hash != new_hash:
        mark_clip_artifacts_stale(db, company, "Clip boundaries were adjusted.")
    clip.boundary_hash = new_hash
    clip.status = "ready_to_render" if clip.rendered_hash != new_hash else clip.status
    clip.status_reason = "Boundaries set by operator."
    db.flush()


def mark_clip_artifacts_stale(db: Session, company: Company, reason: str) -> None:
    for a in db.scalars(select(Artifact).where(Artifact.company_id == company.id, Artifact.is_current.is_(True),
                                               Artifact.kind.in_(["clip_mp4", "company_zip", "report_pdf", "report_docx", "email_txt", "manifest_json"]))):
        a.stale, a.stale_reason = True, reason
    from .regenerate import mark_sections_stale

    mark_sections_stale(db, company, ["replay"], reason)


def render(db: Session, clip: Clip, progress=lambda *_: None, should_cancel=lambda: False) -> Artifact:
    if clip.status not in ("ready_to_render", "rendered", "reviewed", "failed", "rendering"):
        raise ValueError(f"Clip is not ready to render ({clip.status}).")
    company = db.get(Company, clip.company_id)
    video = db.get(SourceFile, clip.video_source_id)
    st = get_storage()
    key = f"clips/{company.id}/{clip.variant}-{clip.boundary_hash[:16]}.mp4"
    dst = st.path(key)
    existing = next((a for a in db.scalars(select(Artifact).where(Artifact.company_id == company.id, Artifact.kind == "clip_mp4",
                                                                  Artifact.input_hash == clip.boundary_hash))
                     if (a.meta or {}).get("variant") == clip.variant), None)
    if not dst.exists():
        clip.status = "rendering"
        db.commit()
        ffmpeg.render_clip(st.path(video.storage_key), dst, clip.start_media_s, clip.end_media_s,
                           on_progress=lambda f: progress(f"Rendering clip ({f:.0%})", 0.1 + 0.8 * f), should_cancel=should_cancel)
    progress("Checking the rendered clip", 0.92)
    validation = validate_clip(db, clip, dst)
    clip.validation = validation
    clip.duration_s = validation.get("duration_s")
    clip.storage_key = key
    clip.rendered_hash = clip.boundary_hash
    clip.status = "rendered" if validation["ok"] else "failed"
    clip.status_reason = "" if validation["ok"] else "Validation failed: " + "; ".join(validation["problems"])
    if not validation["ok"]:
        matching.open_review(db, company.event_id, "clip_validation", f"clipval:{clip.id}:{clip.boundary_hash[:8]}",
                             f"{company.display_name}: rendered clip failed validation", "; ".join(validation["problems"]),
                             company_id=company.id, target_type="clip", target_id=clip.id)
    if existing:
        art = existing
    else:
        for old in db.scalars(select(Artifact).where(Artifact.company_id == company.id, Artifact.kind == "clip_mp4",
                                                     Artifact.is_current.is_(True))):
            if (old.meta or {}).get("variant") == clip.variant:
                old.is_current = False
        from ..util import safe_filename

        art = Artifact(event_id=company.event_id, company_id=company.id, kind="clip_mp4", audience="founder",
                       filename=f"{safe_filename(company.display_name)}_PitchForce_clip{'' if clip.variant == 'core' else '_with_score'}.mp4",
                       storage_key=key, input_hash=clip.boundary_hash, meta={"variant": clip.variant})
        db.add(art)
    art.size = dst.stat().st_size
    from ..storage import sha256_file

    art.sha256 = sha256_file(dst)
    art.is_current, art.stale, art.stale_reason = True, False, ""
    art.meta = {**(art.meta or {}), "variant": clip.variant, "duration_s": clip.duration_s,
                "start": clip.start_media_s, "end": clip.end_media_s, "validation": validation}
    db.flush()
    return art


def validate_clip(db: Session, clip: Clip, path: Path) -> dict:
    problems, checks = [], []
    try:
        info = ffmpeg.probe(path)
    except ffmpeg.MediaError as e:
        return {"ok": False, "problems": [str(e)], "checks": []}
    expected = (clip.end_media_s or 0) - (clip.start_media_s or 0)
    src = db.get(SourceFile, clip.video_source_id)
    src_probe = (src.meta or {}).get("probe", {})
    if info.duration_s is None or abs(info.duration_s - expected) > max(1.5, expected * 0.01):
        problems.append(f"Duration {fmt_duration(info.duration_s)} differs from the expected {fmt_duration(expected)}.")
    else:
        checks.append(f"Duration {fmt_duration(info.duration_s)} matches the selected range.")
    if not info.has_video:
        problems.append("No video stream in the clip.")
    else:
        checks.append(f"Video stream present ({info.video_codec}).")
    if src_probe.get("has_audio") and not info.has_audio:
        problems.append("The source has audio but the clip does not.")
    elif info.has_audio:
        checks.append(f"Audio stream present ({info.audio_codec}).")
    # Content checks against the transcript mapping
    company = db.get(Company, clip.company_id)
    segs = _segs(db, company)
    pitch = segs.get("pitch")
    if pitch:
        am = db.scalar(select(AlignmentMap).where(AlignmentMap.video_source_id == clip.video_source_id,
                                                  AlignmentMap.transcript_source_id == pitch.transcript_source_id))
        if am:
            a = alignment_obj(am)
            open_t, _ = a.to_media(min(s.start_s for k, s in segs.items() if k in ("opening_update", "pitch") and s.start_s is not None))
            if open_t is not None and open_t < (clip.start_media_s or 0) - 1.0:
                problems.append(f"The clip starts after the pitch begins (pitch at {fmt_hms(open_t)}, clip at {fmt_hms(clip.start_media_s)}).")
            else:
                checks.append("Opening: the clip includes the start of the pitch.")
            fb = segs.get("feedback")
            if fb and fb.end_s is not None:
                fb_end, _ = a.to_media(fb.end_s)
                if fb_end is not None and fb_end > (clip.end_media_s or 0) + 1.0:
                    problems.append(f"The clip ends before the final feedback ends (feedback ends {fmt_hms(fb_end)}).")
                else:
                    checks.append("Closing: the clip includes the end of the panel feedback.")
            # The next company must not be included
            nxt = None
            for c in db.scalars(select(Company).where(Company.event_id == company.event_id)):
                if c.id == company.id:
                    continue
                for k in ("opening_update", "intro", "pitch"):
                    s = db.scalar(select(CompanySegment).where(CompanySegment.company_id == c.id, CompanySegment.kind == k))
                    if s and s.start_s is not None and pitch.start_s is not None and s.start_s > pitch.start_s:
                        m, _ = a.to_media(s.start_s)
                        if m is not None and (nxt is None or m < nxt[0]):
                            nxt = (m, c.display_name)
            if nxt and clip.variant == "core" and nxt[0] < (clip.end_media_s or 0) - 0.5:
                problems.append(f"The clip runs into the next company ({nxt[1]} starts at {fmt_hms(nxt[0])}).")
            elif nxt:
                checks.append(f"The next company ({nxt[1]}) is not included.")
    # Thumbnails for the operator's visual check
    thumbs = {}
    st = get_storage()
    for label, at in (("start", 0.5), ("end", max(0.0, (info.duration_s or expected) - 1.0))):
        key = f"clips/{clip.company_id}/thumb-{clip.boundary_hash[:12]}-{label}.jpg"
        try:
            ffmpeg.extract_frame(path, at, st.path(key))
            if st.exists(key):
                thumbs[label] = key
        except Exception:  # noqa: BLE001
            pass
    return {"ok": not problems, "problems": problems, "checks": checks, "duration_s": info.duration_s,
            "streams": info.streams, "thumbnails": thumbs, "validated_at": utcnow().isoformat(timespec="seconds")}
