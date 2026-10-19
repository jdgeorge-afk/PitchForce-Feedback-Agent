"""Turn an uploaded source file into database records.

Each ``ingest_*`` function is idempotent for a given source file: re-running it replaces records that
were derived from that file, while operator decisions (aliases, confirmations, overrides) live in
separate rows and are re-applied.
"""
from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..media import ffmpeg
from ..models import (
    AnnouncedTotal,
    Company,
    Event,
    JudgeAttendance,
    Person,
    PollLaunch,
    PollResponse,
    Score,
    SourceFile,
    Utterance,
)
from ..parsing import polls as pollparse
from ..parsing.documents import extract_text
from ..parsing.rosters import (
    attendance_status,
    parse_chat_scores,
    parse_company_roster,
    parse_judge_roster,
    parse_score_value,
    parse_scores_table,
)
from ..parsing.tabular import read_tabular
from ..parsing.transcript import parse_transcript
from ..storage import get_storage
from ..util import clean_text, norm_company, norm_email, norm_name
from . import matching


def _read(src: SourceFile) -> bytes:
    return get_storage().read_bytes(src.storage_key)


def _prov(src: SourceFile, **extra) -> dict:
    return {"source_file_id": src.id, "sha256": src.sha256, "filename": src.original_filename, **extra}


# ---------------------------------------------------------------------------
# Rosters
# ---------------------------------------------------------------------------
def upsert_company(db: Session, event: Event, name: str, slot: int | None, prov: dict, website: str = "") -> Company:
    m = matching.match_company(db, event, name)
    if m.certain and m.target_id:
        c = db.get(Company, m.target_id)
        if slot and not c.slot_order:
            c.slot_order = slot
        return c
    c = Company(event_id=event.id, display_name=clean_text(name, 300), name_raw=name, slot_order=slot or 0,
                website=website or None, provenance=prov)
    db.add(c)
    db.flush()
    return c


def ingest_company_roster(db: Session, event: Event, src: SourceFile) -> dict:
    res = parse_company_roster(read_tabular(_read(src), src.original_filename))
    created_c, created_p = 0, 0
    for i, row in enumerate(res.rows, start=1):
        v = row.values
        cname = v.get("company", "").strip()
        if not cname:
            res.warnings.append(f"{row.ref}: no company name; row skipped")
            continue
        slot = None
        if v.get("slot", "").strip().isdigit():
            slot = int(v["slot"])
        before = db.scalar(select(Company.id).where(Company.event_id == event.id, Company.name_raw == cname))
        company = upsert_company(db, event, cname, slot or i, _prov(src, ref=row.ref, row=row.row_number), v.get("website", ""))
        created_c += 0 if before else 1
        pname = v.get("presenter", "").strip()
        if pname:
            existing = db.scalar(select(Person).where(Person.event_id == event.id, Person.company_id == company.id,
                                                       Person.role == "presenter", Person.name_raw == pname))
            if not existing:
                email = norm_email(v.get("email"))
                db.add(Person(
                    event_id=event.id, display_name=clean_text(pname, 300), name_raw=pname, email=email or None,
                    email_raw=v.get("email") or None, phone=v.get("phone") or None, title=v.get("title") or None,
                    affiliation=cname, role="presenter", company_id=company.id, source_file_id=src.id,
                    source_ref={"ref": row.ref, "row": row.row_number}, original=row.original,
                ))
                created_p += 1
    db.flush()
    return {"companies_created": created_c, "presenters_created": created_p, "rows": len(res.rows), "warnings": res.warnings}


def ingest_judge_roster(db: Session, event: Event, src: SourceFile) -> dict:
    res = parse_judge_roster(read_tabular(_read(src), src.original_filename))
    created = 0
    for row in res.rows:
        v = row.values
        name = v.get("name", "").strip()
        if not name:
            continue
        p = db.scalar(select(Person).where(Person.event_id == event.id, Person.role == "judge", Person.name_raw == name))
        if not p:
            email = norm_email(v.get("email"))
            p = Person(
                event_id=event.id, display_name=clean_text(name, 300), name_raw=name, email=email or None,
                email_raw=v.get("email") or None, phone=v.get("phone") or None,
                affiliation=v.get("affiliation") or None, title=v.get("title") or None, role="judge",
                source_file_id=src.id, source_ref={"ref": row.ref, "row": row.row_number}, original=row.original,
            )
            db.add(p)
            db.flush()
            created += 1
        status = attendance_status(v.get("attendance", ""))
        att = db.scalar(select(JudgeAttendance).where(JudgeAttendance.event_id == event.id, JudgeAttendance.person_id == p.id))
        if not att:
            db.add(JudgeAttendance(event_id=event.id, person_id=p.id, status=status, source="roster",
                                   evidence={"raw": v.get("attendance", ""), "ref": row.ref}))
        elif att.source == "roster" and att.confirmed_by is None:
            att.status = status
            att.evidence = {"raw": v.get("attendance", ""), "ref": row.ref}
    db.flush()
    n_judges = db.query(Person).filter(Person.event_id == event.id, Person.role == "judge").count()
    if n_judges != event.expected_judge_count:
        matching.open_review(
            db, event.id, "source_conflict", f"judge-count:{n_judges}:{event.expected_judge_count}",
            f"Judge roster lists {n_judges} judges; the event expects {event.expected_judge_count}",
            "Check the expected judge count in event settings, or add the missing judges.", severity="medium",
        )
    return {"judges_created": created, "rows": len(res.rows), "warnings": res.warnings}


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------
def ingest_transcript(db: Session, event: Event, src: SourceFile) -> dict:
    parsed = parse_transcript(_read(src), src.original_filename)
    # Keep operator company assignments / speaker corrections from a previous parse of the same file.
    keep = {u.seq: (u.company_id, u.assignment_source, u.speaker_override)
            for u in db.scalars(select(Utterance).where(Utterance.source_file_id == src.id))
            if u.assignment_source == "operator" or u.speaker_override}
    db.execute(delete(Utterance).where(Utterance.source_file_id == src.id))
    for pu in parsed.utterances:
        u = Utterance(
            event_id=event.id, source_file_id=src.id, seq=pu.seq, cue_ref=pu.cue_ref, speaker_label=pu.speaker,
            start_raw=pu.start_raw, end_raw=pu.end_raw, start_s=pu.start_s, end_s=pu.end_s,
            text=clean_text(pu.text), is_gap=pu.is_gap,
        )
        if pu.seq in keep:
            cid, srcflag, override = keep[pu.seq]
            if srcflag == "operator":
                u.company_id, u.assignment_source = cid, "operator"
            u.speaker_override = override
        db.add(u)
    if not src.meta.get("time_basis_override"):
        src.time_basis = parsed.time_basis
    src.meta = {**src.meta, "format": parsed.format, "speakers": parsed.speakers}
    db.flush()
    matching.map_speakers(db, event, parsed.speakers)
    gaps = sum(1 for u in parsed.utterances if u.is_gap)
    times = [u.start_s for u in parsed.utterances if u.start_s is not None]
    return {
        "utterances": len(parsed.utterances), "speakers": len(parsed.speakers), "format": parsed.format,
        "time_basis": src.time_basis, "gaps": gaps, "warnings": parsed.warnings,
        "first_time": parsed.utterances[0].start_raw if parsed.utterances else "",
        "last_time": parsed.utterances[-1].start_raw if parsed.utterances else "",
        "span_s": (max(times) - min(times)) if times else 0,
    }


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------
def resolve_score_identities(db: Session, event: Event) -> None:
    """(Re)resolve company and judge for every score; flag unresolved ones for review."""
    for s in db.scalars(select(Score).where(Score.event_id == event.id)):
        if s.status in ("rejected",) or s.source_kind == "manual":
            continue
        cm = matching.match_company(db, event, s.company_label_raw)
        jm = matching.match_person(db, event, s.judge_label_raw, roles=("judge",))
        s.company_id = cm.target_id if cm.certain else None
        s.judge_person_id = jm.target_id if jm.certain else None
        if s.status in ("invalid", "out_of_range", "missing"):
            if s.status != "missing" and s.company_id:
                matching.open_review(
                    db, event.id, "score", f"score-invalid:{s.id}",
                    f"Invalid score “{s.value_raw}” from {s.judge_label_raw} for {s.company_label_raw}",
                    s.status_reason + ". It is excluded from the total; correct the source or enter the right score by hand.",
                    company_id=s.company_id, evidence={"ref": s.source_ref}, severity="high", target_type="score", target_id=s.id)
            continue
        if not cm.certain:
            s.status, s.status_reason = "unresolved_company", cm.reason
            matching.open_review(
                db, event.id, "score", f"score-company:{norm_company(s.company_label_raw)}",
                f"Score sheet company “{s.company_label_raw}” is not matched to a presenting company",
                cm.reason, evidence={"ref": s.source_ref, "proposed_company_id": cm.target_id}, severity="high",
                target_type="score_company_label", target_id=s.id,
            )
        elif not jm.certain:
            s.status, s.status_reason = "unresolved_identity", jm.reason
            matching.open_review(
                db, event.id, "score", f"score-judge:{norm_name(s.judge_label_raw)}",
                f"Score sheet judge “{s.judge_label_raw}” is not matched to a judge on the roster",
                jm.reason, evidence={"ref": s.source_ref, "proposed_person_id": jm.target_id}, severity="high",
                target_type="score_judge_label", target_id=s.id,
            )
        else:
            s.status, s.status_reason = "valid", ""
    db.flush()
    from .scoring import mark_duplicates

    mark_duplicates(db, event)


def ingest_scores(db: Session, event: Event, src: SourceFile) -> dict:
    data = _read(src)
    if src.kind == "chat" or src.original_filename.lower().endswith(".txt"):
        res = parse_chat_scores(data)
    else:
        res = parse_scores_table(read_tabular(data, src.original_filename))
    db.execute(delete(Score).where(Score.source_file_id == src.id))
    db.execute(delete(AnnouncedTotal).where(AnnouncedTotal.source_file_id == src.id))
    counts = {"valid": 0, "missing": 0, "invalid": 0, "out_of_range": 0}
    for r in res.scores:
        value, status, reason = parse_score_value(r.value_raw, event.score_min, event.score_max, event.allow_fractional_scores)
        counts[status] = counts.get(status, 0) + 1
        if status == "missing":
            # Blank cell: missing source data. Recorded, never treated as absence.
            pass
        db.add(Score(event_id=event.id, company_label_raw=r.company_label, judge_label_raw=r.judge_label,
                     value_raw=r.value_raw, value=value, status=status, status_reason=reason,
                     source_kind=r.source_kind, source_file_id=src.id, source_ref={"ref": r.ref, "sha256": src.sha256}))
    db.flush()
    resolve_score_identities(db, event)
    for t in res.totals:
        cm = matching.match_company(db, event, t.company_label)
        if cm.certain:
            v = re.match(r"\s*(\d+(?:[.,]\d+)?)", t.value_raw)
            db.add(AnnouncedTotal(company_id=cm.target_id, value_raw=t.value_raw,
                                  value=float(v.group(1).replace(",", ".")) if v else None,
                                  denominator=event.expected_judge_count * event.score_max,
                                  source_kind="file", source_file_id=src.id, excerpt=f"Total column in {src.original_filename}",
                                  at_raw=t.ref, status="candidate"))
    for c in db.scalars(select(Company).where(Company.event_id == event.id)):
        from .scoring import compute_for_company

        compute_for_company(db, event, c)
    return {"layout": res.layout, "scores": len(res.scores), "file_totals": len(res.totals), "by_status": counts, "warnings": res.warnings}


# ---------------------------------------------------------------------------
# Interest polls
# ---------------------------------------------------------------------------
def ingest_interest(db: Session, event: Event, src: SourceFile) -> dict:
    parsed = pollparse.parse_poll_sheets(read_tabular(_read(src), src.original_filename))
    # Replace records from this file but keep operator outreach/status edits keyed by original row reference.
    kept = {(r.original_response_id, r.source_ref.get("ref")): (r.outreach_status, r.outreach_note)
            for r in db.scalars(select(PollResponse).where(PollResponse.source_file_id == src.id))}
    db.execute(delete(PollResponse).where(PollResponse.source_file_id == src.id))
    old_launch_company = {
        (l.launch_index, l.start_raw): (l.company_id, l.company_status, l.timezone_status)
        for l in db.scalars(select(PollLaunch).where(PollLaunch.source_file_id == src.id))
    }
    db.execute(delete(PollLaunch).where(PollLaunch.source_file_id == src.id))
    launches = []
    for rl in parsed.launches:
        start = pollparse.parse_datetime(rl.start_raw)
        end = pollparse.parse_datetime(rl.end_raw)
        pl = PollLaunch(
            event_id=event.id, source_file_id=src.id, launch_index=rl.index, poll_name=rl.poll_name or parsed.overview.get("Poll Name", "") or parsed.overview.get("Title", ""),
            webinar_id_raw=rl.meeting_id_raw, webinar_id=pollparse.normalize_meeting_id(rl.meeting_id_raw),
            start_raw=rl.start_raw, start_at=start, end_raw=rl.end_raw, end_at=end,
            timezone_assumed=parsed.timezone_hint or event.timezone,
            timezone_status="stated" if parsed.timezone_hint else "assumed",
            source_ref={"ref": rl.ref, "topic": rl.topic},
        )
        prev = old_launch_company.get((rl.index, rl.start_raw))
        if prev and prev[1] == "confirmed":
            pl.company_id, pl.company_status = prev[0], "confirmed"
        if prev and prev[2] == "confirmed":
            pl.timezone_status = "confirmed"
        db.add(pl)
        launches.append(pl)
    db.flush()
    for rr in parsed.responses:
        name = clean_text(rr.name, 300)
        email_raw = rr.email
        anonymous = not name.strip() or norm_name(name) in {"anonymous", "anon", "guest", "unknown"}
        pr = PollResponse(
            event_id=event.id, source_file_id=src.id, original_response_id=rr.rid, name_raw=rr.name, email_raw=email_raw,
            name="" if anonymous else name, email=norm_email(email_raw), submitted_raw=rr.submitted_raw,
            submitted_at=pollparse.parse_datetime(rr.submitted_raw), question_raw=rr.question, answer_raw=rr.answer,
            is_anonymous=anonymous, source_ref={"ref": rr.ref, "sha256": src.sha256, "meeting_id_raw": rr.meeting_id_raw,
                                                "launch_raw": rr.launch_raw, "poll_name": rr.poll_name, **rr.extra},
            original_row=rr.original,
        )
        k = (rr.rid, rr.ref)
        if k in kept:
            pr.outreach_status, pr.outreach_note = kept[k]
        for pos, choice in enumerate(pollparse.split_choices(rr.answer)):
            pr.choices.append(_choice(db, pos, choice))
        db.add(pr)
    db.flush()
    from .interest import process_event_interest

    summary = process_event_interest(db, event)
    return {
        "layout": parsed.layout, "launches": len(parsed.launches), "responses": len(parsed.responses),
        "timezone_hint": parsed.timezone_hint, "warnings": parsed.warnings, **summary.get(src.id, {}),
    }


def _choice(db: Session, pos: int, choice: str):
    from ..models import PollResponseChoice
    from .interest import category_rules

    return PollResponseChoice(position=pos, choice_raw=choice, choice_normalized=re.sub(r"\s+", " ", choice).strip(),
                              category=pollparse.categorize(choice, category_rules(db)))


# ---------------------------------------------------------------------------
# Video and documents
# ---------------------------------------------------------------------------
def ingest_video(db: Session, event: Event, src: SourceFile) -> dict:
    path = get_storage().path(src.storage_key)
    info = ffmpeg.probe(Path(path))
    src.meta = {**src.meta, "probe": info.as_dict()}
    if not info.has_audio:
        matching.open_review(db, event.id, "source_conflict", f"video-noaudio:{src.id}",
                             f"Video {src.original_filename} has no audio track", "Clips will be silent.", severity="medium")
    return {"duration_s": info.duration_s, "video": info.video_codec, "audio": info.audio_codec,
            "resolution": f"{info.width}x{info.height}" if info.width else ""}


def ingest_document(db: Session, event: Event | None, src: SourceFile) -> dict:
    text, meta = extract_text(_read(src), src.original_filename)
    key = get_storage().make_key(f"derived/{src.id}", ".txt")
    get_storage().put_bytes(key, text.encode("utf-8"))
    src.meta = {**src.meta, **meta, "text_key": key, "chars": len(text)}
    return {"chars": len(text), **meta}


INGESTERS = {
    "company_roster": ingest_company_roster,
    "judge_roster": ingest_judge_roster,
    "transcript": ingest_transcript,
    "scores": ingest_scores,
    "chat": ingest_scores,
    "interest": ingest_interest,
    "video": ingest_video,
    "deck": ingest_document,
    "factsheet": ingest_document,
    "guidelines": ingest_document,
}


def ingest_source(db: Session, src: SourceFile) -> dict:
    event = db.get(Event, src.event_id) if src.event_id else None
    fn = INGESTERS.get(src.kind)
    if fn is None:
        src.status = "parsed"
        src.parse_summary = {"note": "Stored for reference; not parsed."}
        return src.parse_summary
    summary = fn(db, event, src)
    src.status = "parsed"
    src.parse_summary = summary
    src.parse_error = None
    src.parsed_at = utcnow()
    db.flush()
    return summary
