"""Founder report composition.

Each section is generated from a defined set of inputs. Its ``input_hash`` records exactly what it was
built from, so regeneration only touches sections whose inputs changed, and never overwrites an
operator's edit or approval (those sections are flagged stale instead).

Section text uses a tiny markup shared by the HTML, PDF and DOCX renderers:
  blank line = paragraph, "- " = bullet, "### " = sub-heading, **bold**, [label](url).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analysis.heuristics import THEME_LABELS
from ..config import get_settings
from ..models import (
    INTEREST_LABELS,
    REPORT_SECTIONS,
    AlignmentMap,
    AppSetting,
    CalendarRecommendation,
    CalendarSession,
    Claim,
    Clip,
    Company,
    CompanySegment,
    Event,
    Feedback,
    Person,
    Question,
    ReportSection,
)
from ..services import bestpractices, interest
from ..services.scoring import current_result, fmt_score
from ..util import fmt_duration, fmt_hms, stable_hash

THEME_WHY = {
    "timeline_spend": "Investors fund milestones, not activities. Seeing what each tranche of capital buys, and when, is how they judge whether the raise is sized correctly.",
    "answer_length": "Q&A time is short. Long answers crowd out other questions and can read as uncertainty, even when the content is strong.",
    "ip_competition": "Defensibility determines whether early progress turns into lasting value; investors need to see what stops a better-funded competitor.",
    "investor_return": "Every investor has to underwrite how and when they get their money back. Without an exit and liquidity picture, interest stalls at diligence.",
    "slides": "Dense slides compete with you for attention; panelists skim instead of listening.",
    "regulatory": "Regulatory path drives timeline, cost, and risk, so it shapes valuation and the size of the round.",
    "market": "Investors need to believe a specific customer will buy, at a price that supports the business.",
    "traction": "Evidence of demand is the strongest de-risking signal at this stage.",
    "team": "Investors back teams; gaps that are not addressed become diligence concerns.",
    "financials": "Clear, consistent terms and use of funds make it easy for an interested investor to take the next step.",
    "clarity": "Many investors on the panel and in the audience are not specialists in your field; if they cannot follow, they cannot champion you.",
    "other": "The panel raised this as something that affected their assessment.",
}
THEME_CHANGE = {
    "timeline_spend": "Put the milestone timeline and the spending plan on one slide, so each milestone shows its cost and date.",
    "answer_length": "Lead every answer with a one-sentence direct answer, add one supporting fact, then stop and invite a follow-up.",
    "ip_competition": "State what is protected (issued/pending, scope) and what else makes you hard to copy, then name the realistic competitors.",
    "investor_return": "Add a short exit and liquidity view: likely acquirer types, recent comparable transactions, and the expected time horizon.",
    "slides": "Cut each slide to one message, with fewer words and larger visuals; move detail to an appendix.",
    "regulatory": "Show the regulatory pathway as a short timeline with the key decision points and what de-risks each step.",
    "market": "Name the first customer segment, the buyer, and the price point, with one proof point.",
    "traction": "Lead with your strongest evidence of demand and how it is trending.",
    "team": "Show how the team covers the critical risks and which hires the raise funds.",
    "financials": "State the raise, instrument, and use of funds in one place, consistent with your deck and profile.",
    "clarity": "Explain the product in one plain-language sentence before any technical detail.",
    "other": "Address the point directly in your next pitch and follow-up materials.",
}
THEME_BETTER = {
    "timeline_spend": "A one-page plan: “$X funds A by month 6, B by month 12; that de-risks C for the next round.”",
    "answer_length": "A 20–30 second answer that addresses the question asked, followed by an offer to go deeper.",
    "ip_competition": "One slide: protection, defensibility beyond patents, and a competitor comparison on the dimensions that matter to buyers.",
    "investor_return": "A slide with 3–5 comparable exits, plausible acquirers, and a realistic time to liquidity.",
    "slides": "Slides a non-specialist can read in five seconds each, supporting what you say rather than repeating it.",
    "regulatory": "A pathway graphic with dates, required studies, and the cost of each step.",
    "market": "A bottom-up market slide grounded in real customer conversations.",
    "traction": "Metrics with dates and growth rates, separating signed commitments from pipeline.",
    "team": "A team slide mapped to the company's key risks, with planned hires.",
    "financials": "A single ‘the ask’ slide repeated consistently in every document you share.",
    "clarity": "A plain-English problem/solution statement any investor could repeat to a partner.",
    "other": "A specific, evidence-backed response to the panel's point.",
}
ADVISORY_SERVICES = [
    ("Investor Presentation Strategy", {"slides", "clarity", "answer_length", "market"}, "a clearer pitch"),
    ("Capitalization & Capital Planning", {"timeline_spend", "financials", "investor_return"}, "a funding plan investors can underwrite"),
    ("Due Diligence Preparation", {"regulatory", "ip_competition", "traction", "team"}, "diligence-ready materials"),
]


def _approx_words(text: str) -> int:
    return len(text.split())


# ---------------------------------------------------------------------------
class Evidence:
    """Collects everything sections are built from (and what the AI provider may see)."""

    def __init__(self, db: Session, event: Event, company: Company, now: datetime | None = None):
        self.db, self.event, self.company = db, event, company
        self.now = now or datetime.utcnow()
        self.presenters = list(db.scalars(select(Person).where(Person.company_id == company.id, Person.role == "presenter")))
        self.segs = {s.kind: s for s in db.scalars(select(CompanySegment).where(CompanySegment.company_id == company.id))}
        self.questions = [q for q in db.scalars(select(Question).where(Question.company_id == company.id, Question.parent_id.is_(None))
                                                .order_by(Question.seq)) if q.status != "rejected"]
        self.feedback = [f for f in db.scalars(select(Feedback).where(Feedback.company_id == company.id).order_by(Feedback.seq))
                         if f.status != "rejected"]
        self.claims = list(db.scalars(select(Claim).where(Claim.company_id == company.id)))
        self.score = current_result(db, company.id)
        self.interest = interest.summarize(db, event, company)
        self.clip = db.scalar(select(Clip).where(Clip.company_id == company.id, Clip.variant == "core"))
        self.recs = list(db.scalars(select(CalendarRecommendation).where(CalendarRecommendation.company_id == company.id,
                                                                         CalendarRecommendation.status != "rejected")))
        self.deck_reviewed = False  # decks are not visually inspected by this application

    @property
    def presenter_name(self) -> str:
        return " and ".join(p.display_name for p in self.presenters) or (self.pitch_speaker or "the presenter")

    @property
    def pitch_speaker(self) -> str:
        pitch = self.segs.get("pitch")
        if pitch and pitch.evidence:
            return ""
        return ""

    def clock_label(self) -> str:
        """Which clock the transcript times use, so evidence times are labeled clearly."""
        from ..models import SourceFile

        seg = self.segs.get("pitch")
        src = self.db.get(SourceFile, seg.transcript_source_id) if seg and seg.transcript_source_id else None
        return "recording time" if src is not None and src.time_basis == "media" else "event clock"

    def pitch_duration(self) -> float | None:
        p = self.segs.get("pitch")
        if p and p.start_s is not None and p.end_s is not None:
            start = min(s.start_s for k, s in self.segs.items() if k in ("opening_update", "pitch") and s.start_s is not None)
            return p.end_s - start
        return None

    def themes(self) -> list[tuple[str, list[Feedback]]]:
        by: dict[str, list[Feedback]] = defaultdict(list)
        for f in self.feedback:
            if f.kind in ("recommendation", "comment") and f.theme:
                by[f.theme].append(f)
        ranked = sorted(by.items(), key=lambda kv: (-len({f.speaker_label for f in kv[1]}), -len(kv[1]),
                                                    min(f.at_s or 0 for f in kv[1])))
        return [(k, v) for k, v in ranked if k != "other"] + [(k, v) for k, v in ranked if k == "other"]

    def theme_keys(self) -> list[str]:
        keys = [k for k, _ in self.themes() if k != "other"]
        for q in self.questions:
            from ..analysis.heuristics import theme_of

            t = theme_of(q.original_excerpt)
            if t != "other" and t not in keys and q.unresolved:
                keys.append(t)
        return keys

    def local_time(self, dt_utc: datetime | None) -> str:
        if not dt_utc:
            return ""
        try:
            tz = ZoneInfo(self.event.timezone)
            d = dt_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
            return d.strftime("%a %b %d, %Y, %I:%M %p ").replace(" 0", " ") + d.tzname()
        except Exception:
            return dt_utc.strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
def s_header(ev: Evidence) -> tuple[str, dict]:
    inputs = {"company": ev.company.display_name, "presenters": [p.display_name for p in ev.presenters],
              "event": ev.event.name, "date": str(ev.event.event_date), "sample": ev.event.is_sample}
    lines = [f"**Company:** {ev.company.display_name}",
             f"**Presenter:** {ev.presenter_name}",
             f"**Event:** {ev.event.name}",
             f"**Date:** {ev.event.event_date:%B %d, %Y}".replace(" 0", " ")]
    if ev.event.is_sample:
        lines.append("**SAMPLE REPORT — synthetic data for demonstration only.**")
    return "\n\n".join(lines), inputs


def s_result(ev: Evidence) -> tuple[str, dict]:
    r = ev.score
    d = ev.event.score_display_decimals
    if r is None:
        return "No score information is available for this report.", {"score": None}
    inputs = {k: getattr(r, k) for k in ("status", "valid_count", "raw_sum", "mean", "confirmed_missing", "imputed_contribution",
                                          "adjusted_total", "denominator", "announced_value", "discrepancy", "expected_judges")}
    ann_den = (r.details or {}).get("announced_denominator") or r.denominator
    if r.status == "verified":
        text = (f"**Total score: {fmt_score(r.adjusted_total, d)} / {fmt_score(r.denominator, 0)}** — calculated from "
                f"{r.valid_count} individual judge scores (scale {fmt_score(ev.event.score_min, 0)}–{fmt_score(ev.event.score_max, 0)}).")
    elif r.status == "verified_with_imputation":
        text = (f"**Total score: {fmt_score(r.adjusted_total, d)} / {fmt_score(r.denominator, 0)}** — {r.valid_count} judges scored "
                f"(sum {fmt_score(r.raw_sum, d)}). {r.confirmed_missing} judge{' was' if r.confirmed_missing == 1 else 's were'} absent; "
                f"under the event's scoring rule the average of the submitted scores ({fmt_score(r.mean, 2)}) was added for "
                f"{'that seat' if r.confirmed_missing == 1 else 'each missing seat'}.")
    elif r.status == "announced_only":
        text = (f"**Announced total: {fmt_score(r.announced_value, d)} / {fmt_score(ann_den, 0)}** — as announced at the event. "
                "Individual judge scores were not provided, so this total has not been independently recalculated.")
    elif r.status == "incomplete":
        text = (f"Individual scores were received from {r.valid_count} of {r.expected_judges} judges (sum {fmt_score(r.raw_sum, d)}). "
                "The total is incomplete and is not shown as a final result.")
        if r.announced_value is not None:
            text += f" The announced total was {fmt_score(r.announced_value, d)} / {fmt_score(ann_den, 0)}."
    else:
        text = "No verified score is available for this report."
    if r.discrepancy and abs(r.discrepancy) > 1e-9 and r.status in ("verified", "verified_with_imputation"):
        text += f" Note: the total announced at the event was {fmt_score(r.announced_value, d)}; the verified calculation above is used."
    return text, inputs


def _interest_sentence(ev: Evidence) -> str:
    s = ev.interest
    if s.status != "available":
        return ("Audience poll data is not included: " + s.note) if s.status != "missing_source" else \
            "No audience investment-interest poll data was provided for this event (missing data, not zero interest)."
    parts = []
    for cat in ("invest", "meeting", "due_diligence", "more_info", "introduction", "mentoring", "free_resource"):
        n = s.by_category.get(cat, 0)
        if n:
            parts.append(f"{n} {'respondent' if n == 1 else 'respondents'} chose “{INTEREST_LABELS[cat].lower()}”")
    ni = s.by_category.get("not_interested", 0)
    head = f"In the event poll, {s.respondents} {'person' if s.respondents == 1 else 'people'} responded"
    body = (": " + "; ".join(parts) + ("; " if parts and ni else "") + (f"{ni} said they were not interested" if ni else "")) if (parts or ni) else ""
    tail = ""
    if not s.by_category.get("invest"):
        tail = " No respondent selected investment interest; information requests are not investment commitments."
    return head + body + "." + tail + " " + bestpractices.current(ev.db).content["report_wording"]["interest_reference"]


def s_assessment(ev: Evidence) -> tuple[str, dict]:
    dur = ev.pitch_duration()
    inv_q = [q for q in ev.questions if q.asker_role == "investor"]
    panelists = sorted({f.speaker_label for f in ev.feedback if f.kind not in ("silent",)})
    themes = ev.themes()
    top = [_lc(THEME_LABELS.get(k, k)) for k, _ in themes[:3] if k != "other"]
    unresolved = [q for q in inv_q if q.unresolved]
    inputs = {"dur": dur, "q": [q.id for q in inv_q], "panel": panelists, "themes": top, "unres": len(unresolved),
              "interest": [ev.interest.status, ev.interest.by_category, ev.interest.respondents]}
    s1 = (f"{ev.presenter_name} presented {ev.company.display_name}"
          + (f" in a pitch of about {fmt_duration(dur)}" if dur else "")
          + (f", followed by {len(inv_q)} investor question{'s' if len(inv_q) != 1 else ''}" if inv_q else "")
          + (f" and feedback from {len(panelists)} panelist{'s' if len(panelists) != 1 else ''}." if panelists else "."))
    s2 = (f"The panel's comments centered on {_join(top)}." if top else
          "The panel did not raise a dominant improvement theme in the recorded feedback.")
    s3 = ""
    if unresolved:
        s3 = (f" {len(unresolved)} question{'s were' if len(unresolved) > 1 else ' was'} left partly open, which gives you a natural "
              "reason to follow up with a clear written answer.")
    s4 = ""
    claims = [c for c in ev.claims if c.category in ("regulatory", "efficacy", "funding", "projection", "ip")]
    if claims:
        s4 = (" Statements in the pitch about regulatory status, results, IP, funding, or projections are reported here as your claims; "
              "investors will expect documentation for each.")
    text = s1 + " " + s2 + s3 + s4 + "\n\n" + _interest_sentence(ev)
    return text, inputs


def _lc(label: str) -> str:
    """Lower-case a theme label for use mid-sentence, keeping acronyms (IP, FDA)."""
    return " ".join(w if w.isupper() and len(w) > 1 else w.lower() for w in label.split())


def _join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def s_worked(ev: Evidence) -> tuple[str, dict]:
    pos = [f for f in ev.feedback if f.kind == "positive"]
    seen, picked = set(), []
    for f in pos:
        if f.speaker_label in seen:
            continue
        seen.add(f.speaker_label)
        picked.append(f)
        if len(picked) == 3:
            break
    inputs = {"pos": [(f.id, f.point) for f in picked]}
    if not picked:
        return ("The panel's recorded comments focused on improvements; no specific strengths were called out in the transcript.", inputs)
    lines = [f"- **{f.speaker_label}** ({f.at_raw}): {f.point}" for f in picked]
    return "\n".join(lines), inputs


def s_questions(ev: Evidence) -> tuple[str, dict]:
    inv = [q for q in ev.questions if q.asker_role == "investor"]
    top = sorted(inv, key=lambda q: (-q.importance, q.seq))[:4]
    top.sort(key=lambda q: q.seq)
    inputs = {"q": [(q.id, q.paraphrase, q.unresolved, q.completeness) for q in top], "n": len(inv)}
    if not inv:
        return "No investor questions were identified in the transcript for this session.", inputs
    lines = []
    for q in top:
        status = q.answered_summary or "No answer recorded."
        if q.unresolved:
            status += f" Open point: {q.unresolved}"
        if q.completeness == "gap":
            status += " (Part of this exchange is missing from the recording.)"
        lines.append(f"- **{q.paraphrase}** — {q.asker_label}, {q.at_raw}. {status}")
    extra = len(inv) - len(top)
    text = "\n".join(lines)
    if extra > 0:
        text += f"\n\n{extra} further question{'s are' if extra > 1 else ' is'} listed in the evidence appendix."
    return text, inputs


def s_priorities(ev: Evidence, max_items: int = 3) -> tuple[str, dict]:
    themes = [(k, v) for k, v in ev.themes() if k != "other"]
    inputs = {"themes": [(k, [(f.id, f.point) for f in v]) for k, v in themes[:max_items]]}
    blocks = []
    for n, (k, items) in enumerate(themes[:max_items], start=1):
        by_speaker = []
        for f in items:
            if f.speaker_label not in [s for s, _ in by_speaker]:
                by_speaker.append((f.speaker_label, f))
        said = "; ".join(f"{s} — “{f.point.rstrip('.')}”" for s, f in by_speaker[:3])
        times = ", ".join(sorted({f.at_raw for f in items if f.at_raw}))
        if times:
            times += f" ({ev.clock_label()})"
        attrib = " (panel comment; the deck itself was not reviewed by this report)" if k == "slides" and not ev.deck_reviewed else ""
        blocks.append(
            f"### {n}. {THEME_LABELS.get(k, k)}\n\n"
            f"**What came up:** {said}{attrib}.\n\n"
            f"**Why it matters to investors:** {THEME_WHY[k]}\n\n"
            f"**What to change:** {THEME_CHANGE[k]}\n\n"
            f"**A stronger version:** {THEME_BETTER[k]}\n\n"
            f"**Evidence:** {times or 'see appendix'}"
        )
    if len(blocks) < max_items:
        open_q = [q for q in ev.questions if q.asker_role == "investor" and q.unresolved]
        for q in open_q[: max_items - len(blocks)]:
            n = len(blocks) + 1
            blocks.append(
                f"### {n}. Close the open question: {q.paraphrase.rstrip('?')}\n\n"
                f"**What came up:** {q.asker_label} asked this at {q.at_raw}; {q.unresolved[0].lower() + q.unresolved[1:] if q.unresolved else ''}\n\n"
                "**Why it matters to investors:** Unanswered questions become diligence blockers if they are not closed quickly.\n\n"
                "**What to change:** Send a short written answer with supporting data to the investors who asked or expressed interest.\n\n"
                "**A stronger version:** A one-paragraph answer plus one exhibit, ready before your next investor conversation.\n\n"
                f"**Evidence:** {q.at_raw} ({ev.clock_label()})"
            )
            inputs.setdefault("open_q", []).append(q.id)
    if not blocks:
        return "The recorded feedback did not identify specific improvement priorities for this session.", inputs
    if len(blocks) < max_items:
        blocks.append(f"_Only {len(blocks)} distinct priorit{'y' if len(blocks) == 1 else 'ies'} emerged from the panel's comments; "
                      "no others are added to avoid generic advice._")
    return "\n\n".join(blocks), inputs


def s_best_practices(ev: Evidence) -> tuple[str, dict]:
    lines, version = bestpractices.for_report(ev.db, ev.event, ev.theme_keys())
    intro = bestpractices.current(ev.db).content["report_wording"]["best_practices_intro"]
    inputs = {"lines": lines, "version": version, "format": ev.event.format}
    return intro + "\n\n" + "\n".join(f"- {ln}" for ln in lines), inputs


def s_replay(ev: Evidence) -> tuple[str, dict]:
    c = ev.clip
    inputs = {"clip": [c.status, c.duration_s, c.share_url, c.start_media_s, c.rendered_hash] if c else None,
              "replay_link": ev.event.replay_link}
    parts = []
    if c and c.status in ("rendered", "reviewed", "shared") and c.duration_s:
        if c.share_url:
            parts.append(f"Watch your session (pitch through panel feedback, {fmt_duration(c.duration_s)}): [{c.share_url}]({c.share_url})")
        else:
            parts.append(f"A video clip of your session (pitch through panel feedback, {fmt_duration(c.duration_s)}) is included with this report as an MP4 file.")
        if c.start_media_s is not None:
            parts.append(f"In the full event recording, your session starts at {fmt_hms(c.start_media_s)} (recording time).")
    else:
        parts.append("A video clip of your session is not available for this report.")
    if ev.event.replay_link:
        parts.append(f"Full event replay page: [{ev.event.replay_link}]({ev.event.replay_link})")
    return "\n\n".join(parts), inputs


def s_learning(ev: Evidence) -> tuple[str, dict]:
    from ..services.calendar import cache_status

    cal_url = get_settings().calendar_url_list[0] if get_settings().calendar_url_list else "https://events.ecovg.com/events"
    rows = []
    for r in ev.recs:
        sess = ev.db.get(CalendarSession, r.session_id)
        if sess and sess.start_at_utc and sess.start_at_utc > ev.now and sess.verified_on_detail:
            rows.append((sess, r))
    rows = rows[:3]
    meta = cache_status(ev.db)
    inputs = {"recs": [(s.id, s.title, str(s.start_at_utc), r.reason) for s, r in rows], "cal": cal_url,
              "fetched": meta.get("fetched_at")}
    if not rows:
        return (f"We did not find an upcoming EcoVG session closely related to your feedback when this report was prepared. "
                f"See the official calendar for new sessions: [{cal_url}]({cal_url})"), inputs
    lines = [f"- **{s.title}** — {ev.local_time(s.start_at_utc)}. [{s.detail_url}]({s.detail_url})\n  {r.reason}" for s, r in rows]
    when = meta.get("fetched_at", "")[:10]
    return "\n".join(lines) + (f"\n\n_Listings retrieved from the official EcoVG calendar on {when}._" if when else ""), inputs


def s_advisory(ev: Evidence) -> tuple[str, dict]:
    themes = set(ev.theme_keys())
    if ev.interest.by_category.get("due_diligence"):
        themes.add("regulatory")
    fits = [(name, phrase) for name, keys, phrase in ADVISORY_SERVICES if themes & keys][:2]
    url = get_settings().advisory_url
    setting = ev.db.get(AppSetting, "report_wording")
    enabled = (setting.value.get("advisory_enabled", True) if setting else True)
    inputs = {"fits": fits, "url": url, "enabled": enabled}
    if not enabled or not fits:
        return "", inputs
    names = _join([n for n, _ in fits])
    phrases = _join([p for _, p in fits])
    return (f"If you would like hands-on support turning this feedback into {phrases}, EcoVG offers paid advisory services "
            f"({names}): [{url}]({url}). This is optional and separate from PitchForce judging; it does not affect scores, "
            "introductions, or funding outcomes."), inputs


BUILDERS = {
    "header": s_header, "result": s_result, "assessment": s_assessment, "worked": s_worked, "questions": s_questions,
    "priorities": s_priorities, "best_practices": s_best_practices, "replay": s_replay, "learning": s_learning,
    "advisory": s_advisory,
}


def _ai_prose(ev: Evidence, provider) -> dict | None:
    """Optional AI rewrite of assessment/worked/priorities, validated; returns {key: text} or None."""
    from ..analysis.providers import OfflineProvider, ProviderError

    if provider is None or isinstance(provider, OfflineProvider):
        return None
    evidence = {
        "company": ev.company.display_name, "presenter": ev.presenter_name, "event_format": ev.event.format,
        "deck_reviewed": ev.deck_reviewed,
        "feedback": [{"id": f"u:{f.utterance_id}", "speaker": f.speaker_label, "kind": f.kind, "theme": f.theme, "point": f.point,
                      "at": f.at_raw} for f in ev.feedback],
        "questions": [{"speaker": q.asker_label, "paraphrase": q.paraphrase, "unresolved": q.unresolved, "at": q.at_raw}
                      for q in ev.questions if q.asker_role == "investor"],
    }
    try:
        out = provider.report_prose(evidence)
    except ProviderError:
        return None
    if out is None:
        return None
    known = json.dumps(evidence)
    import re as _re

    for txt in [out.assessment, *out.worked, *[p.issue + p.what_to_change for p in out.priorities]]:
        for num in _re.findall(r"\d+(?:\.\d+)?", txt):
            if num not in known:
                return None  # a number not present in the evidence: reject the AI prose
    valid_ids = {f"u:{f.utterance_id}" for f in ev.feedback}
    blocks = []
    for n, p in enumerate(out.priorities[:3], start=1):
        if not set(p.evidence_utterances) & valid_ids:
            return None
        times = ", ".join(sorted({f.at_raw for f in ev.feedback if f"u:{f.utterance_id}" in p.evidence_utterances}))
        blocks.append(f"### {n}. {p.title}\n\n**What came up:** {p.issue}\n\n**Why it matters to investors:** {p.why_it_matters}\n\n"
                      f"**What to change:** {p.what_to_change}\n\n**A stronger version:** {p.better_version}\n\n**Evidence:** {times}")
    return {"assessment": out.assessment + "\n\n" + _interest_sentence(ev),
            "worked": "\n".join(f"- {w}" for w in out.worked[:3]),
            "priorities": "\n\n".join(blocks)}


def compose(db: Session, event: Event, company: Company, keys: list[str] | None = None, force: bool = False,
            discard_edits: bool = False, user_id: str | None = None, provider=None, now: datetime | None = None) -> dict:
    """(Re)generate report sections. Returns {key: action} where action is created|updated|unchanged|stale_kept."""
    ev = Evidence(db, event, company, now)
    existing = {s.key: s for s in db.scalars(select(ReportSection).where(ReportSection.company_id == company.id))}
    ai = _ai_prose(ev, provider) if provider is not None else None
    actions = {}
    for pos, (key, title) in enumerate(REPORT_SECTIONS):
        if keys and key not in keys:
            continue
        text, inputs = BUILDERS[key](ev)
        meta = {"generator": "template-v1"}
        if ai and key in ai and ai[key].strip():
            text = ai[key]
            meta = {"generator": "ai", "provider": provider.name, "model": provider.model}
        h = stable_hash(inputs)
        sec = existing.get(key)
        if sec is None:
            sec = ReportSection(company_id=company.id, key=key, position=pos, title=title, generated_text=text, input_hash=h,
                                model_meta=meta)
            db.add(sec)
            actions[key] = "created"
            continue
        operator_owned = sec.edited_text is not None or sec.approved
        if operator_owned and not discard_edits:
            if h != sec.input_hash:
                sec.generated_text = text  # keep the fresh draft available for comparison
                sec.input_hash = h
                sec.model_meta = meta
                sec.stale = True
                sec.stale_reason = "The evidence behind this section changed after it was edited/approved. Your text was kept; review it."
                actions[key] = "stale_kept"
            else:
                actions[key] = "unchanged"
            continue
        if discard_edits:
            sec.edited_text = None
            sec.approved = False
        if h != sec.input_hash or force or sec.generated_text != text:
            sec.generated_text, sec.input_hash, sec.model_meta = text, h, meta
            sec.stale, sec.stale_reason = False, ""
            sec.updated_by = user_id
            actions[key] = "updated"
        else:
            sec.stale, sec.stale_reason = False, ""
            actions[key] = "unchanged"
    db.flush()
    return actions


def sections(db: Session, company: Company) -> list[ReportSection]:
    return list(db.scalars(select(ReportSection).where(ReportSection.company_id == company.id).order_by(ReportSection.position)))


def report_word_count(db: Session, company: Company) -> int:
    return sum(_approx_words(s.text) for s in sections(db, company) if s.key not in ("header",))


_ = AlignmentMap
