"""Analysis pipeline: event-level segmentation and per-company extraction, persisted with provenance.

Stage 1 (event): pick the primary transcript, segment it into presentation blocks, match blocks to
roster companies (proposals need review), store segments, announced totals and review items.
Stage 2 (per company, isolated): questions, feedback, founder claims, score result, then report
sections and clip candidates (see services/regenerate.py).

Operator decisions are never overwritten: confirmed segments, operator utterance assignments,
speaker overrides, confirmed/rejected aliases, and rejected items are all re-applied.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import (
    AIUsage,
    AnnouncedTotal,
    Claim,
    Company,
    CompanySegment,
    Event,
    Feedback,
    JudgeAttendance,
    Person,
    Question,
    SourceFile,
    SpeakerMapping,
    Utterance,
)
from ..services import matching
from ..util import fmt_hms, norm_company, norm_name, stable_hash
from . import heuristics as H
from .providers import CompanyContext, OfflineProvider, ProviderError, get_provider
from .validate import validate_feedback, validate_questions, validate_segments


# ---------------------------------------------------------------------------
def primary_transcript(db: Session, event: Event) -> SourceFile | None:
    srcs = list(db.scalars(select(SourceFile).where(SourceFile.event_id == event.id, SourceFile.kind == "transcript",
                                                    SourceFile.status == "parsed")))
    if not srcs:
        return None
    for s in srcs:
        if (s.meta or {}).get("primary"):
            return s

    def score(s: SourceFile):
        n = db.query(Utterance).filter(Utterance.source_file_id == s.id, Utterance.speaker_label != "").count()
        return (n, 1 if s.time_basis == "media" else 0)

    return max(srcs, key=score)


def load_uvs(db: Session, src: SourceFile) -> tuple[list[H.UV], list[Utterance]]:
    rows = list(db.scalars(select(Utterance).where(Utterance.source_file_id == src.id).order_by(Utterance.seq)))
    uvs = []
    for i, u in enumerate(rows):
        end = u.end_s
        if end is None and u.start_s is not None:
            end = u.start_s + max(2.0, len(u.text.split()) / 2.5)
        uvs.append(H.UV(id=u.id, idx=i, t=u.start_s, end=end, raw=u.start_raw, speaker=u.speaker, text=u.text, is_gap=u.is_gap))
    return uvs, rows


def speaker_info(db: Session, event: Event) -> dict[str, H.SpeakerInfo]:
    info = {}
    for m in db.scalars(select(SpeakerMapping).where(SpeakerMapping.event_id == event.id)):
        p = db.get(Person, m.person_id) if m.person_id else None
        certain = m.status in ("confirmed", "confirmed_auto")
        role = m.role if (certain or m.role == "host") else "unknown"
        info[m.speaker_label] = H.SpeakerInfo(m.speaker_label, role, p.id if p and certain else None,
                                              p.company_id if p and certain else None, certain)
    return info


@dataclass
class BlockMatch:
    company_id: str | None
    status: str  # matched | proposed | unmatched
    evidence: list[str]


def _labels(db: Session, c: Company) -> list[str]:
    return matching.labels_for_company(db, c)


def _match_block(db: Session, event: Event, b: H.Block, uvs: list[H.UV], rows: list[Utterance],
                 info: dict[str, H.SpeakerInfo], companies: list[Company]) -> BlockMatch:
    ev: list[tuple[str, str, str]] = []  # (company_id, strength, detail)
    rng = rows[b.block_start:b.block_end + 1]
    op = [u.company_id for u in rng if u.assignment_source == "operator" and u.company_id]
    if op:
        cid = max(set(op), key=op.count)
        ev.append((cid, "operator", "Operator assigned these transcript lines to this company"))
    si = info.get(b.presenter)
    if si and si.company_id and si.certain:
        ev.append((si.company_id, "certain", f"Speaker “{b.presenter}” is the roster presenter"))
    intro = b.intro_text + " " + " ".join(u.text for u in uvs[b.pitch_start:b.pitch_start + 2])
    for c in companies:
        for a in [c.display_name] + [x.alias for x in c.aliases if x.status == "confirmed"]:
            k = norm_company(a)
            if k and len(k) >= 3 and k in norm_company(intro):
                ev.append((c.id, "certain", f"Introduction names “{a}”"))
                break
        for x in c.aliases:
            if x.status == "proposed" and norm_company(x.alias) and norm_company(x.alias) in norm_company(intro):
                ev.append((c.id, "proposed", f"Introduction names “{x.alias}”, a proposed alias of {c.display_name}"))
    pm = matching.match_person(db, event, b.presenter, roles=("presenter",))
    if pm.target_id and pm.status == "proposed":
        p = db.get(Person, pm.target_id)
        if p and p.company_id:
            ev.append((p.company_id, "proposed", f"Speaker “{b.presenter}” resembles roster presenter “{p.display_name}”"))
    op_ids = {e[0] for e in ev if e[1] == "operator"}
    if len(op_ids) == 1:
        return BlockMatch(next(iter(op_ids)), "matched", [e[2] for e in ev])
    ids = {e[0] for e in ev if e[1] == "certain"}
    if len(ids) == 1:
        return BlockMatch(next(iter(ids)), "matched", [e[2] for e in ev])
    if len(ids) > 1:
        return BlockMatch(None, "conflict", [e[2] for e in ev])
    ids = {e[0] for e in ev if e[1] == "proposed"}
    if len(ids) == 1:
        return BlockMatch(next(iter(ids)), "proposed", [e[2] for e in ev])
    return BlockMatch(None, "unmatched", [e[2] for e in ev])


def _rejected_pair(db: Session, event: Event, presenter: str, company_id: str) -> bool:
    """True when the operator already said this presentation is not this company."""
    from ..models import ReviewItem

    for key in (f"block-slot:{presenter}:{company_id}", f"block-company:{presenter}:{company_id}"):
        it = db.scalar(select(ReviewItem).where(ReviewItem.event_id == event.id, ReviewItem.dedupe_key == key))
        if it and (it.evidence or {}).get("resolution") == "reject":
            return True
    return False


def _seg_times(uvs: list[H.UV], b: H.Boundary) -> tuple[float | None, float | None]:
    s = uvs[b.start_idx].t if b.start_idx is not None else None
    e = uvs[b.end_idx].end if b.end_idx is not None else None
    return s, e


ABSENCE = re.compile(
    r"\b([A-Z][a-z]+)\s+(?i:couldn'?t|could not|can'?t|cannot|is unable to|was unable to|wasn'?t able to|isn'?t able to)"
    r"\s+(?i:join|attend|make it|be here|be with us)"
)


def analyze_event(db: Session, event: Event, progress=lambda *_: None, provider=None) -> dict:
    src = primary_transcript(db, event)
    companies = list(db.scalars(select(Company).where(Company.event_id == event.id).order_by(Company.slot_order)))
    if src is None:
        matching.open_review(db, event.id, "missing_source", "no-transcript", "No transcript uploaded",
                             "Feedback, questions, and clip boundaries need a transcript. Upload a TXT/VTT/SRT transcript, "
                             "or configure a transcription provider to transcribe the video.", severity="high")
        return {"blocks": 0, "note": "no transcript"}
    progress("Reading transcript", 0.1)
    for t in db.scalars(select(SourceFile).where(SourceFile.event_id == event.id, SourceFile.kind == "transcript",
                                                 SourceFile.status == "parsed")):
        matching.map_speakers(db, event, (t.meta or {}).get("speakers", []))
    uvs, rows = load_uvs(db, src)
    info = speaker_info(db, event)
    progress("Finding each company's presentation", 0.25)
    all_labels = [lab for c in companies for lab in _labels(db, c)]
    blocks, hosts, judges = H.segment_event(uvs, info, all_labels)
    # Mark hosts/judges in speaker mappings when inferred (proposals only).
    for sp in hosts:
        m = db.scalar(select(SpeakerMapping).where(SpeakerMapping.event_id == event.id, SpeakerMapping.speaker_label == sp))
        if m and m.status not in ("confirmed", "confirmed_auto"):
            m.role = "host"
    result = {"blocks": len(blocks), "matched": 0, "proposed": 0, "unmatched": 0, "transcript": src.original_filename}
    used: set[str] = set()
    block_for: dict[str, H.Block] = {}
    pending = []
    for b in blocks:
        bm = _match_block(db, event, b, uvs, rows, info, companies)
        if bm.company_id and bm.status != "matched" and _rejected_pair(db, event, b.presenter, bm.company_id):
            bm = BlockMatch(None, "unmatched", bm.evidence + ["operator rejected the proposed match"])
        if bm.company_id and bm.company_id not in used:
            used.add(bm.company_id)
            block_for[bm.company_id] = b
            b.notes.append(f"match:{bm.status}")
            b.match_evidence = bm.evidence  # type: ignore[attr-defined]
            result["matched" if bm.status == "matched" else "proposed"] += 1
            if bm.status == "matched":
                _unknown_name_proposals(db, event, db.get(Company, bm.company_id), b, hosts, judges)
            else:
                c = db.get(Company, bm.company_id)
                matching.open_review(
                    db, event.id, "company_match", f"block-company:{b.presenter}:{bm.company_id}",
                    f"Is the presentation by “{b.presenter}” the roster company “{c.display_name}”?",
                    "The match is a proposal: " + "; ".join(bm.evidence), company_id=c.id,
                    evidence={"speaker": b.presenter, "evidence": bm.evidence, "at": uvs[b.pitch_start].raw}, severity="high",
                    target_type="company", target_id=c.id,
                    options=[{"value": "confirm", "label": "Yes, same company"}, {"value": "reject", "label": "No, different company"}],
                )
        else:
            pending.append((b, bm))
    # Slot-order proposals for remaining blocks (weak evidence: same slot does not prove identity).
    remaining = [c for c in companies if c.id not in used]
    for b, bm in pending:
        if not remaining:
            matching.open_review(db, event.id, "company_match", f"block-unknown:{b.presenter}:{uvs[b.pitch_start].raw}",
                                 f"Presentation by “{b.presenter}” at {uvs[b.pitch_start].raw} does not match any roster company",
                                 "Add the company to the roster or assign these transcript lines to an existing company.",
                                 evidence={"names": b.names_mentioned}, severity="high")
            result["unmatched"] += 1
            continue
        # choose the remaining company whose slot is closest to this block's order
        order = blocks.index(b) + 1
        allowed = [x for x in remaining if not _rejected_pair(db, event, b.presenter, x.id)]
        if not allowed:
            matching.open_review(db, event.id, "company_match", f"block-unknown:{b.presenter}:{uvs[b.pitch_start].raw}",
                                 f"Presentation by “{b.presenter}” at {uvs[b.pitch_start].raw} is not assigned to a company",
                                 "Proposed matches were rejected. Assign these transcript lines to a company in its workspace "
                                 "(Transcript tab) or add the company to the roster.", evidence={"names": b.names_mentioned},
                                 severity="high")
            result["unmatched"] += 1
            continue
        c = min(allowed, key=lambda x: abs((x.slot_order or 99) - order))
        remaining.remove(c)
        used.add(c.id)
        block_for[c.id] = b
        b.notes.append("match:proposed")
        result["proposed"] += 1
        names = [n for n in b.names_mentioned if norm_name(n) not in {norm_name(x) for x in (b.presenter, *hosts, *judges)}
                 and not any(norm_name(n) in norm_name(x) for x in (b.presenter, *hosts, *judges))]
        presenter = db.scalar(select(Person).where(Person.company_id == c.id, Person.role == "presenter"))
        detail = (f"Only the presentation order suggests this match (slot {c.slot_order}). Transcript names: "
                  f"{', '.join(names) or 'none found'}; speaker “{b.presenter}”. Roster: “{c.display_name}”"
                  + (f" presented by “{presenter.display_name}”." if presenter else "."))
        for n in names[:2]:
            matching.add_company_alias(db, c, n, "transcript", evidence={"intro": b.intro_text[:300], "reason": "slot order"})
        if presenter and norm_name(b.presenter) != norm_name(presenter.display_name):
            matching.add_person_alias(db, presenter, b.presenter, "transcript", evidence={"reason": "slot order"})
        matching.open_review(
            db, event.id, "company_match", f"block-slot:{b.presenter}:{c.id}",
            f"Transcript presenter “{b.presenter}”{' / ' + names[0] if names else ''} vs roster “{presenter.display_name if presenter else '?'}” / “{c.display_name}”",
            detail, company_id=c.id, evidence={"speaker": b.presenter, "names": names, "at": uvs[b.pitch_start].raw},
            severity="high", target_type="company", target_id=c.id,
            options=[{"value": "confirm", "label": "Same company and presenter (record aliases)"},
                     {"value": "reject", "label": "Different company"}],
        )
    for c in companies:
        if c.id not in block_for:
            matching.open_review(db, event.id, "company_match", f"company-missing:{c.id}",
                                 f"“{c.display_name}” was not found in the transcript",
                                 "Check the roster, upload the correct transcript, or assign transcript lines to this company.",
                                 company_id=c.id, severity="high", target_type="company", target_id=c.id)

    db.commit()  # keep write transactions short (SQLite allows one writer at a time)
    progress("Saving boundaries", 0.6)
    # Reset automatic utterance assignments; keep operator ones.
    for u in rows:
        if u.assignment_source != "operator":
            u.company_id = None
    awards_labels = {}
    for cid, b in block_for.items():
        c = db.get(Company, cid)
        for u in rows[b.block_start:b.block_end + 1]:
            if u.assignment_source != "operator":
                u.company_id = cid
        status_prefix = "proposed" if "match:proposed" in b.notes else "matched"
        awards_labels[cid] = _labels(db, c) + [b.presenter]
        segs = dict(b.segments)
        # AI refinement of boundaries (validated); offline provider returns None.
        prov = provider or get_provider()
        if not isinstance(prov, OfflineProvider):
            ctx = CompanyContext(c.display_name, b.presenter, uvs[b.block_start:b.block_end + 1], hosts, judges, [], segs,
                                 event.format, utts_full=uvs)
            try:
                refined = prov.refine_segments(ctx)
                if refined:
                    ok, new_segs, notes = validate_segments(refined, ctx, uvs)
                    if ok:
                        segs.update(new_segs)
                    b.notes.extend(notes)
            except ProviderError as e:
                b.notes.append(f"AI boundary refinement unavailable: {e}")
            _record_usage(db, event, c, ctx.usages)
        for kind, bd in segs.items():
            existing = db.scalar(select(CompanySegment).where(CompanySegment.company_id == cid, CompanySegment.kind == kind))
            if existing and existing.status == "confirmed":
                continue
            s_t, e_t = _seg_times(uvs, bd)
            seg = existing or CompanySegment(company_id=cid, kind=kind)
            seg.transcript_source_id = src.id
            seg.start_utt_id = uvs[bd.start_idx].id if bd.start_idx is not None else None
            seg.end_utt_id = uvs[bd.end_idx].id if bd.end_idx is not None else None
            seg.start_s, seg.end_s = s_t, e_t
            seg.confidence = bd.confidence
            seg.method = bd.method
            seg.status = "candidate" if (bd.confidence >= 0.7 and status_prefix == "matched") else "needs_review"
            seg.evidence = {"note": bd.note, "start_raw": uvs[bd.start_idx].raw if bd.start_idx is not None else "",
                            "end_raw": uvs[bd.end_idx].raw if bd.end_idx is not None else "", "block_notes": b.notes,
                            "match_evidence": getattr(b, "match_evidence", [])}
            if not existing:
                db.add(seg)
            if seg.status == "needs_review":
                matching.open_review(
                    db, event.id, "boundary", f"boundary:{cid}:{kind}",
                    f"{c.display_name}: check the {kind.replace('_', ' ')} boundary",
                    f"Detected {uvs[bd.start_idx].raw if bd.start_idx is not None else '?'}–{uvs[bd.end_idx].raw if bd.end_idx is not None else '?'} "
                    f"({bd.method}). " + " ".join(n for n in b.notes if not n.startswith("match:")),
                    company_id=cid, target_type="segment", target_id=seg.id or "", severity="medium",
                )
        # Remove stale automatic segments that no longer exist (keep confirmed)
        for seg in db.scalars(select(CompanySegment).where(CompanySegment.company_id == cid)):
            if seg.kind not in segs and seg.kind != "award" and seg.status != "confirmed":
                db.delete(seg)
        # Announced total from transcript
        if b.announced:
            u = rows[b.announced["idx"]]
            at = db.scalar(select(AnnouncedTotal).where(AnnouncedTotal.company_id == cid, AnnouncedTotal.utterance_id == u.id))
            if not at:
                db.add(AnnouncedTotal(
                    company_id=cid, value_raw=str(b.announced["value"]).rstrip("0").rstrip(".") if b.announced["value"] % 1 else str(int(b.announced["value"])),
                    value=b.announced["value"], denominator=b.announced["denominator"] or event.expected_judge_count * event.score_max,
                    source_kind="transcript", source_file_id=src.id, utterance_id=u.id, speaker=u.speaker, excerpt=u.text,
                    at_raw=f"{u.start_raw}" + (f"–{u.end_raw}" if u.end_raw else ""), status="candidate",
                ))
    # Awards: separate segment per company
    for cid, labels in awards_labels.items():
        idxs = H.find_awards(uvs, labels, hosts)
        seg = db.scalar(select(CompanySegment).where(CompanySegment.company_id == cid, CompanySegment.kind == "award"))
        if idxs and not (seg and seg.status == "confirmed"):
            i = idxs[0]
            seg = seg or CompanySegment(company_id=cid, kind="award")
            seg.transcript_source_id = src.id
            seg.start_utt_id = seg.end_utt_id = uvs[i].id
            seg.start_s, seg.end_s = uvs[i].t, uvs[i].end
            seg.confidence, seg.method, seg.status = 0.6, "award mention", "candidate"
            seg.evidence = {"excerpt": uvs[i].text, "start_raw": uvs[i].raw}
            db.add(seg)
    # Judge absence mentioned by host -> proposal for operator confirmation
    judges_people = list(db.scalars(select(Person).where(Person.event_id == event.id, Person.role == "judge")))
    for u in uvs:
        if u.speaker in hosts:
            for m in ABSENCE.finditer(u.text):
                first = m.group(1)
                cands = [p for p in judges_people if norm_name(p.display_name).split(" ")[0] == norm_name(first)]
                matching.open_review(
                    db, event.id, "score", f"absence:{norm_name(first)}",
                    f"Host said “{first}” could not attend. Confirm judge absence?",
                    f"“{u.text}” ({u.raw}). " + (f"Possible roster judge: {', '.join(p.display_name for p in cands)}. "
                                                 if cands else "No roster judge with that first name; identity unresolved. ")
                    + "Confirming absence allows the approved missing-judge scoring rule to apply.",
                    evidence={"utterance_id": u.id, "excerpt": u.text, "at": u.raw, "candidates": [p.id for p in cands]},
                    target_type="judge_absence", target_id=cands[0].id if len(cands) == 1 else "", severity="high",
                )
    # Gaps
    gaps = [u for u in uvs if u.is_gap]
    if gaps:
        matching.open_review(db, event.id, "source_conflict", f"gaps:{src.id}:{len(gaps)}",
                             f"Transcript has {len(gaps)} gap(s) (inaudible/missing audio)",
                             "Gaps are shown in the evidence view and are not reconstructed. First gap at " + gaps[0].raw,
                             severity="low", evidence={"at": [g.raw for g in gaps[:20]]})
    db.flush()
    result["hosts"] = sorted(hosts)
    result["judges"] = sorted(judges)
    event.notes = event.notes  # touch
    return result


REL_OPTIONS = [("same_company", "Same company (record as alias)"), ("product_name", "Product name"),
               ("trading_name", "Trading name"), ("parent", "Parent company"), ("subsidiary", "Subsidiary"),
               ("licensor", "Licensor (licenses technology to the company)"), ("licensee", "Licensee"),
               ("unrelated", "Not related / transcription error")]


def _candidate_org_names(b: H.Block, people_labels: list[str]) -> list[str]:
    plabels = {norm_name(x) for x in people_labels if x}
    parts = {p for x in plabels for p in x.split(" ")}
    out = []
    for n in b.names_mentioned:
        k = norm_name(n)
        if not k or k in plabels or all(w in parts for w in k.split(" ")):
            continue
        out.append(n)
    return out


def _unknown_name_proposals(db: Session, event: Event, company: Company, b: H.Block, hosts: set[str], judges: set[str]) -> None:
    """A matched block whose introduction uses a different organization name: propose a relationship
    (never an automatic merge)."""
    people = [b.presenter, *hosts, *judges] + [p.display_name for p in db.scalars(select(Person).where(Person.event_id == event.id))]
    known = {norm_company(x) for x in matching.labels_for_company(db, company)}
    for n in _candidate_org_names(b, people)[:2]:
        k = norm_company(n)
        if k in known or any(k in kn or kn in k for kn in known if len(kn) >= 4):
            continue
        rejected = {norm_company(a.alias) for a in company.aliases if a.status == "rejected"}
        if k in rejected:
            continue
        r = matching.propose_relationship(db, event, company, n, company.display_name, "unknown",
                                          {"intro": b.intro_text[:300], "note": f"Introduction says “{n}”; roster lists “{company.display_name}”."})
        if r.status != "proposed":
            continue
        matching.open_review(
            db, event.id, "related_entity", f"rel:{company.id}:{k}",
            f"{company.display_name}: introduced as “{n}”. How are they related?",
            f"Introduction: “{b.intro_text[:240]}”. The names are not treated as interchangeable until you confirm.",
            company_id=company.id, evidence={"relationship_id": r.id}, target_type="relationship", target_id=r.id,
            options=[{"value": v, "label": lbl} for v, lbl in REL_OPTIONS],
        )


def _record_usage(db: Session, event: Event, company: Company | None, usages, job_id: str | None = None) -> float:
    total = 0.0
    for us in usages:
        if us.provider == "offline":
            continue
        db.add(AIUsage(event_id=event.id, company_id=company.id if company else None, job_id=job_id, provider=us.provider,
                       model=us.model, purpose=us.purpose, prompt_version=us.prompt_version, input_tokens=us.input_tokens,
                       output_tokens=us.output_tokens, est_cost_usd=us.est_cost_usd))
        total += us.est_cost_usd
    return total


# ---------------------------------------------------------------------------
def company_context(db: Session, event: Event, company: Company) -> tuple[CompanyContext | None, list[H.UV], list[Utterance]]:
    src = primary_transcript(db, event)
    if not src:
        return None, [], []
    uvs, rows = load_uvs(db, src)
    idx_of = {u.id: u.idx for u in uvs}
    segs = {s.kind: s for s in db.scalars(select(CompanySegment).where(CompanySegment.company_id == company.id))}
    if not segs:
        return None, uvs, rows
    bds = {k: H.Boundary(idx_of.get(s.start_utt_id), idx_of.get(s.end_utt_id), s.confidence, s.method) for k, s in segs.items()}
    starts = [b.start_idx for b in bds.values() if b.start_idx is not None]
    ends = [b.end_idx for b in bds.values() if b.end_idx is not None]
    lo, hi = (min(starts), max(ends)) if starts and ends else (0, len(uvs) - 1)
    info = speaker_info(db, event)
    hosts = {s for s, i in info.items() if i.role == "host"} | {
        m.speaker_label for m in db.scalars(select(SpeakerMapping).where(SpeakerMapping.event_id == event.id, SpeakerMapping.role == "host"))}
    judge_labels = {s for s, i in info.items() if i.role == "judge"}
    pitch = bds.get("pitch")
    presenter = uvs[pitch.start_idx].speaker if pitch and pitch.start_idx is not None else ""
    # Treat non-host, non-presenter speakers who speak in Q&A/feedback across the event as judges.
    all_companies = list(db.scalars(select(Company).where(Company.event_id == event.id)))
    presenters = set()
    for c in all_companies:
        p = db.scalar(select(CompanySegment).where(CompanySegment.company_id == c.id, CompanySegment.kind == "pitch"))
        if p and p.start_utt_id in idx_of:
            presenters.add(uvs[idx_of[p.start_utt_id]].speaker)
    by_company: dict[str, set] = {}
    for u in rows:
        if u.company_id and u.speaker and u.speaker not in presenters and u.speaker not in hosts:
            by_company.setdefault(u.speaker, set()).add(u.company_id)
    judge_labels |= {s for s, cs in by_company.items() if len(cs) >= 2 or len(all_companies) == 1}
    # Present judges: judge speakers heard anywhere in the event, excluding confirmed-absent people.
    absent_people = {a.person_id for a in db.scalars(select(JudgeAttendance).where(
        JudgeAttendance.event_id == event.id, JudgeAttendance.status.in_(["absent_confirmed"])))}
    present = sorted(s for s in judge_labels if not (info.get(s) and info[s].person_id in absent_people))
    ctx = CompanyContext(company.display_name, presenter, uvs[lo:hi + 1], hosts, judge_labels, present, bds, event.format,
                         utts_full=uvs)
    return ctx, uvs, rows


def analyze_company(db: Session, event: Event, company: Company, provider=None, progress=lambda *_: None) -> dict:
    ctx, uvs, rows = company_context(db, event, company)
    if ctx is None:
        return {"status": "no_segments"}
    prov = provider or get_provider()
    meta = {"provider": prov.name, "model": prov.model}
    notes = []
    progress("Extracting investor questions", 0.2)
    try:
        qout = prov.questions(ctx)
    except ProviderError as e:
        notes.append(f"AI question extraction failed ({e}); offline extraction used.")
        qout = OfflineProvider().questions(ctx)
        meta = {"provider": "offline (fallback)", "model": OfflineProvider.model}
    qs, qnotes = validate_questions(qout, ctx)
    notes += qnotes
    progress("Extracting panel feedback", 0.45)
    try:
        fout = prov.feedback(ctx)
    except ProviderError as e:
        notes.append(f"AI feedback extraction failed ({e}); offline extraction used.")
        fout = OfflineProvider().feedback(ctx)
    fbs, fnotes = validate_feedback(fout, ctx)
    notes += fnotes
    _record_usage(db, event, company, ctx.usages)

    # Persist questions/feedback, preserving operator-edited/confirmed/rejected rows.
    keep_q = {q.utterance_id: q for q in db.scalars(select(Question).where(Question.company_id == company.id))
              if q.status in ("confirmed", "edited", "rejected")}
    db.execute(delete(Question).where(Question.company_id == company.id, Question.status == "extracted"))
    for q in qs:
        u = uvs[q["utt_idx"]]
        if u.id in keep_q:
            continue
        parent = Question(
            company_id=company.id, seq=q["seq"], asker_label=u.speaker, asker_role=q["asker_role"],
            asker_person_id=_person_for_label(db, event, u.speaker), is_followup=q["is_followup"], utterance_id=u.id,
            at_s=u.t, at_raw=u.raw, original_excerpt=u.text, paraphrase=q["paraphrase"],
            answer_utterance_ids=[uvs[i].id for i in q["answer_idxs"]],
            answer_excerpt=" ".join(uvs[i].text for i in q["answer_idxs"])[:2000],
            answer_at_raw=uvs[q["answer_idxs"][0]].raw if q["answer_idxs"] else "",
            answered_summary=q["answered"], unresolved=q["unresolved"], completeness=q["completeness"],
            importance=q["importance"],
            provenance={"transcript_source_id": rows[0].source_file_id if rows else None, "cue": rows[q["utt_idx"]].cue_ref,
                        "speaker": u.speaker, "interval": [u.t, u.end], **meta, "origin": "investor" if q["asker_role"] == "investor" else q["asker_role"]},
        )
        db.add(parent)
        db.flush()
        for j, part in enumerate(q["parts"]):
            db.add(Question(company_id=company.id, parent_id=parent.id, part_label=part["label"], seq=q["seq"],
                            asker_label=u.speaker, asker_role=q["asker_role"], utterance_id=u.id, at_s=u.t, at_raw=u.raw,
                            original_excerpt=u.text, paraphrase=part["text"], completeness=q["completeness"],
                            provenance={"part_of": parent.id, **meta}))
    keep_f = {(f.utterance_id, f.point) for f in db.scalars(select(Feedback).where(Feedback.company_id == company.id))
              if f.status in ("confirmed", "edited", "rejected")}
    db.execute(delete(Feedback).where(Feedback.company_id == company.id, Feedback.status == "extracted"))
    for n, f in enumerate(fbs):
        u = uvs[f["utt_idx"]] if f["utt_idx"] is not None else None
        if (u.id if u else None, f["point"]) in keep_f:
            continue
        db.add(Feedback(
            company_id=company.id, seq=n, person_id=_person_for_label(db, event, f["speaker"]), speaker_label=f["speaker"],
            kind=f["kind"], point=f["point"], theme=f["theme"], original_excerpt=u.text if u else "",
            utterance_id=u.id if u else None, at_s=u.t if u else None, at_raw=u.raw if u else "",
            provenance={"speaker": f["speaker"], "interval": [u.t, u.end] if u else None, "cue": rows[u.idx].cue_ref if u else None,
                        "origin": "investor", **meta},
        ))
    # Founder claims (never verified automatically)
    db.execute(delete(Claim).where(Claim.company_id == company.id, Claim.origin == "founder_claim", Claim.verified.is_(False)))
    pitch = ctx.segments.get("pitch")
    if pitch:
        for idx, cat, sent in H.founder_claims(uvs, pitch, ctx.presenter):
            db.add(Claim(company_id=company.id, origin="founder_claim", category=cat, text=sent, original_excerpt=uvs[idx].text,
                         utterance_id=uvs[idx].id, at_raw=uvs[idx].raw, verified=False,
                         provenance={"speaker": ctx.presenter, "note": "Presenter statement; not independently verified"}))
    # Relationship cues in the presenter's speech (parent, licensor...)
    _relationship_proposals(db, event, company, ctx, uvs)
    db.flush()
    return {"questions": len(qs), "feedback": len(fbs), "notes": notes,
            "input_hash": stable_hash([qs, fbs])}


def _person_for_label(db: Session, event: Event, label: str) -> str | None:
    m = db.scalar(select(SpeakerMapping).where(SpeakerMapping.event_id == event.id, SpeakerMapping.speaker_label == label))
    if m and m.person_id and m.status in ("confirmed", "confirmed_auto"):
        return m.person_id
    return None


def _relationship_proposals(db: Session, event: Event, company: Company, ctx: CompanyContext, uvs: list[H.UV]) -> None:
    roster_names = {norm_company(company.display_name)}
    texts = [u for u in ctx.utts if u.speaker == ctx.presenter or u.speaker in ctx.hosts]
    for u in texts:
        for pat, rel in matching.RELATION_CUES:
            for m in pat.finditer(u.text):
                obj = m.group(1).strip()
                if norm_company(obj) in roster_names or len(norm_company(obj)) < 3:
                    continue
                r = matching.propose_relationship(
                    db, event, company, obj, company.display_name, rel,
                    {"excerpt": u.text, "at": u.raw, "utterance_id": u.id,
                     "note": f"Presenter mentioned “{obj}” ({rel.replace('_', ' ')}); roster lists “{company.display_name}”."},
                )
                from rapidfuzz import fuzz

                similar = fuzz.ratio(norm_company(obj), norm_company(company.display_name)) >= 75
                matching.open_review(
                    db, event.id, "related_entity", f"rel:{company.id}:{norm_company(obj)}",
                    f"{company.display_name}: how is “{obj}” related?",
                    f"At {u.raw} the presenter said: “{u.text[:240]}”. "
                    + (f"“{obj}” may be a transcription of the roster name “{company.display_name}”. " if similar else "")
                    + "Related entities are not merged or treated as interchangeable without your confirmation.",
                    company_id=company.id, evidence={"utterance_id": u.id, "relationship_id": r.id},
                    target_type="relationship", target_id=r.id,
                    options=[{"value": v, "label": lbl} for v, lbl in [
                        ("parent", f"{obj} is the parent company"), ("licensor", f"{obj} licenses technology to the company"),
                        ("subsidiary", f"{obj} is a subsidiary"), ("product_name", f"{obj} is a product name"),
                        ("trading_name", f"{obj} is a trading name"), ("same_company", "Same company (alias)"),
                        ("unrelated", "Not related / transcription error")]],
                )


_ = fmt_hms
