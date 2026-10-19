"""Deterministic validation of provider output against the source transcript.

AI (or rule-based) output is accepted only when every reference resolves to a real utterance in the
company's block, speakers match the utterance's speaker, and ranges are ordered. Invalid items are
dropped with a note; excerpts are always copied from the source, never from model text.
"""
from __future__ import annotations

from . import heuristics as H
from .providers import CompanyContext
from .schemas import FeedbackOut, QuestionsOut, SegmentsOut


def _idx(ctx: CompanyContext, ref: str | None, allowed: set[int]) -> int | None:
    i = ctx.parse_local(ref)
    return i if i is not None and i in allowed else None


def validate_segments(out: SegmentsOut, ctx: CompanyContext, uvs: list[H.UV]) -> tuple[bool, dict[str, H.Boundary], list[str]]:
    allowed = {u.idx for u in ctx.utts}
    segs: dict[str, H.Boundary] = {}
    notes = []
    for b in out.boundaries:
        s, e = _idx(ctx, b.start_utterance, allowed), _idx(ctx, b.end_utterance, allowed)
        if s is None or e is None or e < s:
            notes.append(f"AI boundary for {b.kind} rejected (invalid utterance reference).")
            continue
        segs[b.kind] = H.Boundary(s, e, b.confidence, "ai:" + b.reason[:80])
    order = ["opening_update", "intro", "pitch", "qa", "feedback", "score"]
    present = [k for k in order if k in segs]
    for a, b in zip(present, present[1:]):
        if segs[a].start_idx > segs[b].start_idx:
            notes.append(f"AI boundaries out of order ({a} after {b}); rule-based boundaries kept.")
            return False, {}, notes
    if "pitch" not in segs:
        notes.append("AI did not return a pitch boundary; rule-based boundaries kept.")
        return False, {}, notes
    pitch_speaker = uvs[segs["pitch"].start_idx].speaker
    if pitch_speaker != ctx.presenter:
        notes.append("AI pitch start is not spoken by the presenter; rule-based boundaries kept.")
        return False, {}, notes
    return True, segs, notes


def validate_questions(out: QuestionsOut, ctx: CompanyContext) -> tuple[list[dict], list[str]]:
    qa = ctx.segments.get("qa")
    allowed = {u.idx for u in ctx.utts}
    if qa and qa.start_idx is not None:
        hi = qa.end_idx if qa.end_idx is not None else qa.start_idx
        allowed = {i for i in allowed if qa.start_idx <= i <= hi + 3}
    res, notes, seq = [], [], 0
    for q in out.questions:
        i = _idx(ctx, q.utterance, allowed)
        if i is None:
            notes.append(f"Dropped a question with an invalid reference ({q.utterance}).")
            continue
        u = ctx.utts_full[i]
        if u.speaker == ctx.presenter:
            notes.append(f"Dropped a 'question' at {u.raw} spoken by the presenter.")
            continue
        role = q.asker_role
        if u.speaker in ctx.hosts and role == "investor":
            role = "moderator"  # deterministic rule: hosts are moderators
        ans = [x for x in (_idx(ctx, a, {v.idx for v in ctx.utts}) for a in q.answer_utterances) if x is not None]
        ans = [a for a in ans if ctx.utts_full[a].speaker == ctx.presenter and a > i]
        completeness = q.completeness
        if u.is_gap or any(ctx.utts_full[a].is_gap for a in ans):
            completeness = "gap"
        elif not ans and completeness == "complete":
            completeness = "incomplete"
        seq += 1
        res.append({
            "seq": seq, "utt_idx": i, "asker_role": role, "paraphrase": q.paraphrase.strip(),
            "parts": [{"label": p.label, "text": p.text} for p in q.parts], "is_followup": q.is_followup,
            "answer_idxs": ans, "answered": q.answered_summary, "completeness": completeness,
            "unresolved": q.unresolved or ("No answer was recorded." if not ans else ""), "importance": q.importance,
        })
    return res, notes


def validate_feedback(out: FeedbackOut, ctx: CompanyContext) -> tuple[list[dict], list[str]]:
    allowed = {u.idx for u in ctx.utts}
    res, notes = [], []
    spoke = set()
    for f in out.items:
        if f.kind == "silent":
            continue
        i = _idx(ctx, f.utterance, allowed)
        if i is None:
            notes.append(f"Dropped feedback with an invalid reference ({f.utterance}).")
            continue
        u = ctx.utts_full[i]
        if u.speaker != f.speaker:
            notes.append(f"Feedback at {u.raw} re-attributed from “{f.speaker}” to the actual speaker “{u.speaker}”.")
        if u.speaker == ctx.presenter:
            notes.append(f"Dropped 'feedback' at {u.raw} spoken by the presenter.")
            continue
        spoke.add(u.speaker)
        res.append({"utt_idx": i, "speaker": u.speaker, "kind": f.kind, "point": f.point.strip(), "theme": f.theme})
    # Silent judges are decided deterministically: present judges with no feedback utterance.
    fb = ctx.segments.get("feedback")
    if fb and fb.start_idx is not None:
        hi = fb.end_idx if fb.end_idx is not None else fb.start_idx
        heard = {ctx.utts_full[i].speaker for i in range(fb.start_idx, hi + 1)}
        for j in ctx.present_judges:
            if j not in heard and j not in spoke:
                res.append({"utt_idx": None, "speaker": j, "kind": "silent", "theme": "other",
                            "point": "No feedback from this panelist was recorded in the transcript for this company."})
    return res, notes
