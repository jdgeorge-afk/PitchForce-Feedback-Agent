"""Deliverable generation: report PDF/DOCX, interest XLSX, email draft, evidence appendix, manifest,
per-company ZIP (founder-facing) and the organizer's event ZIP.

Founder packages are assembled only from company-scoped queries, so they cannot include another
company's report, contacts, scores, or source recordings. The organizer audit workbook, issues
summary and structured JSON live only in the event package.
"""
from __future__ import annotations

import html
import io
import json
import zipfile
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import __version__
from ..models import (
    INTEREST_LABELS,
    AlignmentMap,
    Artifact,
    Claim,
    Clip,
    Company,
    CompanySegment,
    Event,
    Feedback,
    Person,
    Question,
    ReviewItem,
    SourceFile,
)
from ..services import interest as interest_svc
from ..services.regenerate import package_input_hash
from ..services.scoring import current_result, fmt_score
from ..storage import get_storage
from ..util import clean_text, fmt_duration, fmt_hms, safe_filename
from . import compose, markup
from .render import render_docx, render_pdf
from .xlsx import audit_workbook, founder_workbook


def _store(db: Session, event: Event, company: Company | None, kind: str, filename: str, data: bytes, audience: str,
           input_hash: str, job_id: str | None = None, meta: dict | None = None) -> Artifact:
    st = get_storage()
    key = st.make_key(f"artifacts/{event.id}/{company.id if company else 'event'}", "." + filename.rsplit(".", 1)[-1])
    size, digest = st.put_bytes(key, data)
    for old in db.scalars(select(Artifact).where(Artifact.event_id == event.id, Artifact.company_id == (company.id if company else None),
                                                 Artifact.kind == kind, Artifact.is_current.is_(True))):
        if old.input_hash == input_hash and old.sha256 == digest:
            st.delete(key)
            old.stale = False
            return old  # idempotent: identical output already stored
        old.is_current = False
    a = Artifact(event_id=event.id, company_id=company.id if company else None, kind=kind, audience=audience, filename=filename,
                 storage_key=key, sha256=digest, size=size, input_hash=input_hash, is_sample=event.is_sample, job_id=job_id,
                 meta=meta or {})
    db.add(a)
    db.flush()
    return a


def _founder_first(db: Session, company: Company) -> tuple[str, bool]:
    p = db.scalar(select(Person).where(Person.company_id == company.id, Person.role == "presenter"))
    open_match = db.scalar(select(ReviewItem).where(ReviewItem.company_id == company.id, ReviewItem.kind == "company_match",
                                                    ReviewItem.status == "open"))
    if not p or open_match:
        return "[confirm recipient name]", False
    return p.display_name.split(" ")[0], True


def email_draft(db: Session, event: Event, company: Company, included: list[str]) -> str:
    first, verified = _founder_first(db, company)
    s = interest_svc.summarize(db, event, company)
    clip = db.scalar(select(Clip).where(Clip.company_id == company.id, Clip.variant == "core"))
    date = f"{event.event_date:%B %d, %Y}".replace(" 0", " ")
    att = ["your feedback report (PDF, with an editable Word copy)"]
    if "interest_xlsx" in included:
        att.append("a spreadsheet of the follow-up responses from the event poll")
    if "clip_mp4" in included:
        att.append("a video clip of your session, from your pitch through the panel's feedback")
    lines = [f"Subject: Your PitchForce feedback – {event.name} ({date})", "", f"Hi {first},", "",
             f"Thank you for presenting {company.display_name} at {event.name} on {date}. Attached you'll find "
             + compose._join(att) + "."]
    if s.status == "available":
        inv = s.by_category.get("invest", 0)
        info = s.by_category.get("more_info", 0)
        mtg = s.by_category.get("meeting", 0)
        bits = []
        if inv:
            bits.append(f"{inv} {'person' if inv == 1 else 'people'} indicated interest in investing")
        if mtg:
            bits.append(f"{mtg} asked for a meeting")
        if info:
            bits.append(f"{info} asked for more information")
        if bits:
            lines += ["", "In the event poll, " + compose._join(bits) + ". The spreadsheet has the details so you can follow up directly; "
                      "please reach out promptly." + ("" if inv else " (Requests for information are not investment commitments.)")]
    if clip and clip.share_url and "clip_mp4" not in included:
        lines += ["", f"Replay of your session: {clip.share_url}"]
    lines += ["", "The report summarizes the investors' questions and feedback and suggests three priorities for your next pitch "
                  "and follow-up. You own your investor relationships and follow-up; EcoVG is glad to help where useful, but "
                  "responses, meetings, and investments are not guaranteed.", "", "Best regards,", "[Your name]", "Ecosystem Venture Group"]
    if event.is_sample:
        lines = ["*** SAMPLE – synthetic demonstration data, do not send ***", ""] + lines
    if not verified:
        lines = ["*** Recipient not yet verified: confirm the founder's name and email before sending. ***", ""] + lines
    return "\n".join(lines) + "\n"


def evidence_html(db: Session, event: Event, company: Company) -> str:
    e = html.escape
    segs = {s.kind: s for s in db.scalars(select(CompanySegment).where(CompanySegment.company_id == company.id))}
    out = [f"<!doctype html><html><head><meta charset='utf-8'><title>Evidence – {e(company.display_name)}</title>",
           "<style>body{font-family:Segoe UI,Arial,sans-serif;max-width:960px;margin:24px auto;color:#222;line-height:1.45}"
           "h1,h2{color:#1F4E5F}table{border-collapse:collapse;width:100%;font-size:13px}td,th{border:1px solid #ccc;padding:4px 6px;"
           "vertical-align:top;text-align:left}th{background:#eef3f5}.muted{color:#666}.gap{background:#fff3cd}</style></head><body>",
           f"<h1>Evidence appendix – {e(company.display_name)}</h1>",
           f"<p class='muted'>{e(event.name)} · {event.event_date} · generated {datetime.utcnow():%Y-%m-%d %H:%M} UTC · "
           "excerpts are verbatim from the transcript; paraphrases are marked.</p>"]
    if event.is_sample:
        out.append("<p><strong>SAMPLE – synthetic data.</strong></p>")
    out.append("<h2>Session boundaries (transcript time)</h2><table><tr><th>Part</th><th>Start</th><th>End</th><th>Status</th><th>Method</th></tr>")
    for k in ("opening_update", "intro", "pitch", "qa", "feedback", "score", "award"):
        s = segs.get(k)
        if s:
            out.append(f"<tr><td>{k.replace('_', ' ')}</td><td>{e(s.evidence.get('start_raw', ''))}</td><td>{e(s.evidence.get('end_raw', ''))}</td>"
                       f"<td>{e(s.status)}</td><td>{e(s.method)}</td></tr>")
    out.append("</table>")
    out.append("<h2>Investor and moderator questions</h2><table><tr><th>#</th><th>Asked by</th><th>Time</th><th>Original wording</th>"
               "<th>Paraphrase</th><th>Founder answer (excerpt)</th><th>Open points</th></tr>")
    for q in db.scalars(select(Question).where(Question.company_id == company.id, Question.parent_id.is_(None)).order_by(Question.seq)):
        if q.status == "rejected":
            continue
        parts = list(db.scalars(select(Question).where(Question.parent_id == q.id).order_by(Question.part_label)))
        para = e(q.paraphrase) + ("".join(f"<br>({e(p.part_label)}) {e(p.paraphrase)}" for p in parts))
        cls = " class='gap'" if q.completeness != "complete" else ""
        out.append(f"<tr{cls}><td>{q.seq}{' (follow-up)' if q.is_followup else ''}</td><td>{e(q.asker_label)}<br><span class='muted'>"
                   f"{e(q.asker_role)}</span></td><td>{e(q.at_raw)}</td><td>{e(q.original_excerpt)}</td><td>{para}</td>"
                   f"<td>{e(q.answer_excerpt)}<br><span class='muted'>{e(q.answer_at_raw)}</span></td><td>{e(q.unresolved)}"
                   f"{' [' + e(q.completeness) + ']' if q.completeness != 'complete' else ''}</td></tr>")
    out.append("</table><h2>Panel feedback (every extracted point)</h2><table><tr><th>Panelist</th><th>Time</th><th>Type</th>"
               "<th>Point (paraphrase)</th><th>Original excerpt</th></tr>")
    for f in db.scalars(select(Feedback).where(Feedback.company_id == company.id).order_by(Feedback.seq)):
        if f.status == "rejected":
            continue
        out.append(f"<tr><td>{e(f.speaker_label)}</td><td>{e(f.at_raw)}</td><td>{e(f.kind.replace('_', ' '))}</td><td>{e(f.point)}</td>"
                   f"<td>{e(f.original_excerpt)}</td></tr>")
    out.append("</table><h2>Presenter claims (not independently verified)</h2><ul>")
    for c in db.scalars(select(Claim).where(Claim.company_id == company.id)):
        out.append(f"<li>[{e(c.category)}] “{e(c.text)}” <span class='muted'>({e(c.at_raw)}; {'verified by operator' if c.verified else 'unverified claim'})</span></li>")
    out.append("</ul><h2>Score details</h2>")
    r = current_result(db, company.id)
    if r:
        d = r.details or {}
        out.append(f"<p>Status: <b>{e(r.status)}</b>. Expected judges: {r.expected_judges}. Verified scores: {r.valid_count}. "
                   f"Raw sum: {fmt_score(r.raw_sum, 2)}. Imputed contribution: {fmt_score(r.imputed_contribution, 2)} (imputed, not a judge score). "
                   f"Adjusted total: {fmt_score(r.adjusted_total, 2)} / {fmt_score(r.denominator, 0)}. Announced: {fmt_score(r.announced_value, 2)}. "
                   f"Discrepancy: {fmt_score(r.discrepancy, 2)}.</p><p>{e(' '.join(d.get('explanation', [])))}</p>")
        if d.get("valid_scores"):
            out.append("<ul>" + "".join(f"<li>{e(j)}: {v:g}</li>" for j, v in d["valid_scores"]) + "</ul>")
    clip = db.scalar(select(Clip).where(Clip.company_id == company.id, Clip.variant == "core"))
    if clip:
        out.append(f"<h2>Video clip</h2><p>Status: {e(clip.status)}. Range (recording time): {fmt_hms(clip.start_media_s)}–{fmt_hms(clip.end_media_s)}"
                   f" ({fmt_duration((clip.end_media_s or 0) - (clip.start_media_s or 0)) if clip.start_media_s is not None else '—'}). "
                   f"{e(clip.status_reason or '')}</p>")
        if clip.validation:
            out.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in clip.validation.get("checks", []) + clip.validation.get("problems", [])) + "</ul>")
        am = db.scalar(select(AlignmentMap).where(AlignmentMap.video_source_id == clip.video_source_id)) if clip.video_source_id else None
        if am:
            out.append(f"<p class='muted'>Alignment: {e(am.method)} ({e(am.status)}). {e(am.notes or '')}</p>")
    out.append("</body></html>")
    return "\n".join(out)


def build_company_package(db: Session, event: Event, company: Company, include_clip: bool = True, include_evidence: bool = True,
                          job_id: str | None = None, progress=lambda *_: None) -> dict:
    secs = compose.sections(db, company)
    if not secs:
        compose.compose(db, event, company)
        secs = compose.sections(db, company)
    h = package_input_hash(db, company)
    base = safe_filename(company.display_name)
    title = f"PitchForce Feedback – {company.display_name}"
    subtitle = f"{event.name} · {event.event_date:%B %d, %Y}".replace(" 0", " ")
    footer = ("SAMPLE – synthetic data. " if event.is_sample else "") + f"Confidential – prepared for {company.display_name} by EcoVG"
    progress("Rendering PDF report", 0.15)
    pdf = render_pdf(title, subtitle, secs, footer)
    a_pdf = _store(db, event, company, "report_pdf", f"{base}_PitchForce_Feedback.pdf", pdf, "founder", h, job_id)
    progress("Rendering Word report", 0.3)
    docx = render_docx(title, subtitle, secs, footer)
    a_docx = _store(db, event, company, "report_docx", f"{base}_PitchForce_Feedback.docx", docx, "founder", h, job_id)
    included = ["report_pdf", "report_docx"]
    limitations = []
    progress("Building interest workbook", 0.45)
    s = interest_svc.summarize(db, event, company)
    a_xlsx = None
    if s.status == "available":
        xbytes, xmeta = founder_workbook(db, event, company)
        a_xlsx = _store(db, event, company, "interest_xlsx", f"{base}_Investor_Follow-up.xlsx", xbytes, "founder", h, job_id, xmeta)
        included.append("interest_xlsx")
    else:
        limitations.append("Interest workbook not generated: " + s.note)
    clip = db.scalar(select(Clip).where(Clip.company_id == company.id, Clip.variant == "core"))
    clip_art = None
    if clip and clip.status in ("rendered", "reviewed", "shared") and clip.storage_key:
        clip_art = next((a for a in db.scalars(select(Artifact).where(Artifact.company_id == company.id, Artifact.kind == "clip_mp4",
                                                                      Artifact.is_current.is_(True)))
                         if (a.meta or {}).get("variant", "core") == "core"), None)
        if include_clip and clip_art:
            included.append("clip_mp4")
    else:
        limitations.append("Video clip not included: " + (clip.status_reason or clip.status if clip else "no video uploaded") + ".")
    email = email_draft(db, event, company, included)
    a_email = _store(db, event, company, "email_txt", f"{base}_email_draft.txt", email.encode("utf-8"), "founder", h, job_id)
    ev_html = evidence_html(db, event, company) if include_evidence else None
    a_ev = _store(db, event, company, "evidence_html", f"{base}_Evidence_Appendix.html", ev_html.encode("utf-8"), "founder", h, job_id) if ev_html else None
    open_items = list(db.scalars(select(ReviewItem).where(ReviewItem.company_id == company.id, ReviewItem.status == "open")))
    for it in open_items:
        limitations.append(f"Unresolved review item: {it.title}")
    stale = [x.title for x in secs if x.stale]
    if stale:
        limitations.append("Report sections flagged for review after source changes: " + ", ".join(stale))
    sources = list(db.scalars(select(SourceFile).where(SourceFile.event_id == event.id, SourceFile.status == "parsed")))
    files = [a for a in (a_pdf, a_docx, a_xlsx, a_email, a_ev) if a] + ([clip_art] if (clip_art and include_clip) else [])
    manifest = {
        "package": "founder", "company": company.display_name, "event": event.name, "event_date": str(event.event_date),
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z", "app_version": __version__,
        "sample_data": event.is_sample,
        "files": [{"name": a.filename, "kind": a.kind, "sha256": a.sha256, "bytes": a.size} for a in files],
        "source_versions": [{"name": s_.original_filename, "kind": s_.kind, "sha256": s_.sha256} for s_ in sources
                            if s_.kind in ("transcript", "video", "interest", "scores", "company_roster", "judge_roster", "chat")],
        "limitations": limitations,
        "report_word_count": compose.report_word_count(db, company),
        "input_hash": h,
    }
    man_bytes = json.dumps(manifest, indent=2).encode("utf-8")
    a_man = _store(db, event, company, "manifest_json", f"{base}_manifest.json", man_bytes, "founder", h, job_id)
    progress("Packaging ZIP", 0.8)
    st = get_storage()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for a in files + [a_man]:
            if a.kind == "clip_mp4":
                z.write(st.path(a.storage_key), f"{base}/{a.filename}", compress_type=zipfile.ZIP_STORED)
            else:
                z.writestr(f"{base}/{a.filename}", st.read_bytes(a.storage_key))
    zname = f"{base}_PitchForce_Package{'_SAMPLE' if event.is_sample else ''}.zip"
    a_zip = _store(db, event, company, "company_zip", zname, buf.getvalue(), "founder", h, job_id,
                   {"files": [a.filename for a in files + [a_man]]})
    return {"zip": a_zip.id, "files": [a.filename for a in files], "limitations": limitations}


def issues_markdown(db: Session, event: Event) -> str:
    lines = [f"# Review and issues summary – {event.name}", "", f"Generated {datetime.utcnow():%Y-%m-%d %H:%M} UTC", ""]
    items = list(db.scalars(select(ReviewItem).where(ReviewItem.event_id == event.id).order_by(ReviewItem.status, ReviewItem.severity)))
    for status in ("open", "resolved", "dismissed"):
        sub = [i for i in items if i.status == status]
        lines += [f"## {status.title()} ({len(sub)})", ""]
        for i in sub:
            c = db.get(Company, i.company_id) if i.company_id else None
            lines.append(f"- [{i.severity}] {i.title}" + (f" — {c.display_name}" if c else "") + (f": {clean_text(i.detail, 300)}" if i.detail else ""))
        lines.append("")
    return "\n".join(lines)


def structured_export(db: Session, event: Event) -> dict:
    companies = []
    for c in db.scalars(select(Company).where(Company.event_id == event.id).order_by(Company.slot_order)):
        r = current_result(db, c.id)
        s = interest_svc.summarize(db, event, c)
        clip = db.scalar(select(Clip).where(Clip.company_id == c.id, Clip.variant == "core"))
        companies.append({
            "id": c.id, "name": c.display_name, "aliases": [{"alias": a.alias, "status": a.status, "scope": a.scope} for a in c.aliases],
            "presenters": [{"id": p.id, "name": p.display_name} for p in db.scalars(select(Person).where(Person.company_id == c.id))],
            "score": {k: getattr(r, k) for k in ("status", "valid_count", "raw_sum", "imputed_contribution", "adjusted_total",
                                                 "denominator", "announced_value", "discrepancy")} if r else None,
            "interest": {"status": s.status, "respondents": s.respondents, "by_category": {INTEREST_LABELS[k]: v for k, v in s.by_category.items()}},
            "segments": {seg.kind: {"start": seg.evidence.get("start_raw"), "end": seg.evidence.get("end_raw"), "status": seg.status}
                         for seg in db.scalars(select(CompanySegment).where(CompanySegment.company_id == c.id))},
            "questions": [{"by": q.asker_label, "role": q.asker_role, "at": q.at_raw, "paraphrase": q.paraphrase, "unresolved": q.unresolved,
                           "completeness": q.completeness} for q in db.scalars(select(Question).where(Question.company_id == c.id, Question.parent_id.is_(None)))],
            "feedback": [{"by": f.speaker_label, "kind": f.kind, "theme": f.theme, "point": f.point, "at": f.at_raw}
                         for f in db.scalars(select(Feedback).where(Feedback.company_id == c.id))],
            "clip": {"status": clip.status, "start": clip.start_media_s, "end": clip.end_media_s, "duration_s": clip.duration_s} if clip else None,
        })
    return {"schema": "pitchforce-feedback/event-export/v1", "event": {"id": event.id, "name": event.name, "date": str(event.event_date),
            "timezone": event.timezone, "format": event.format, "sample": event.is_sample}, "companies": companies,
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}


def build_event_package(db: Session, event: Event, include_clips: bool = True, job_id: str | None = None, progress=lambda *_: None) -> dict:
    companies = list(db.scalars(select(Company).where(Company.event_id == event.id).order_by(Company.slot_order)))
    st = get_storage()
    buf = io.BytesIO()
    results = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, c in enumerate(companies):
            progress(f"Packaging {c.display_name}", 0.05 + 0.7 * i / max(1, len(companies)))
            try:
                res = build_company_package(db, event, c, include_clip=include_clips, job_id=job_id)
                a = db.get(Artifact, res["zip"])
                z.writestr(f"companies/{a.filename}", st.read_bytes(a.storage_key))
                results[c.display_name] = "ok"
            except Exception as e:  # noqa: BLE001 - one company's failure must not block the others
                results[c.display_name] = f"failed: {e}"
        progress("Building audit workbook", 0.8)
        z.writestr("organizer/Event_Audit_Workbook.xlsx", audit_workbook(db, event))
        z.writestr("organizer/Review_and_Issues_Summary.md", issues_markdown(db, event))
        z.writestr("organizer/structured_export.json", json.dumps(structured_export(db, event), indent=2, default=str))
        z.writestr("organizer/README.txt", "Organizer-only files. Do not forward this folder to founders.\n"
                   + ("SAMPLE – synthetic data.\n" if event.is_sample else ""))
    name = f"{safe_filename(event.name)}_Event_Package.zip"
    a = _store(db, event, None, "event_zip", name, buf.getvalue(), "organizer", "", job_id, {"results": results})
    _store(db, event, None, "audit_xlsx", f"{safe_filename(event.name)}_Audit.xlsx", audit_workbook(db, event), "organizer", "", job_id)
    return {"zip": a.id, "results": results}


_ = markup
