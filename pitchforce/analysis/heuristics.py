"""Deterministic transcript analysis.

This is the offline analysis path (no data leaves the server) and the baseline that AI output is
validated against. It works from conversational structure rather than fixed durations:

1. Presenter detection: speakers whose words are concentrated in one window of the event and who are
   not mapped as judges/hosts.
2. Per presenter block: intro (host cue), optional opening update, pitch, Q&A, feedback, score
   announcement, and award mentions, each found by cues in the text and speaker turns.
3. Questions and feedback are extracted with attribution to the speaker and the utterance.
Everything carries utterance references so excerpts come verbatim from the source.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from ..util import norm_company, norm_name, normalize_punct

INTRO_CUE = re.compile(
    r"\b(next up|up next|our next|first up|our first|next we have|next is|please welcome|welcome|"
    r"let'?s (?:hear|welcome|bring)|take it away|floor is yours|turn it over|over to you|introduce|presenting|whenever you're ready)\b",
    re.I,
)
STRONG_INTRO = re.compile(
    r"\b(next up|up next|our next|first up|our first (?:company|presenter)|next we have|next is|please welcome|"
    r"take it away|floor is yours|over to you|turn it over|whenever you're ready|presented by|our last (?:company|presenter)|"
    r"last but not least)\b",
    re.I,
)
QA_CUE = re.compile(r"\b(questions?|q ?& ?a|open it up|panel|investors?|judges?)\b", re.I)
FEEDBACK_HOST_CUE = re.compile(r"\b(feedback|comments|advice|thoughts for|suggestions)\b", re.I)
FEEDBACK_JUDGE_CUE = re.compile(
    r"\b(my feedback|my advice|my suggestion|my recommendation|i would suggest|i'd suggest|i would recommend|"
    r"i'd recommend|one suggestion|my comment|i'd encourage|i would encourage|nothing (?:else )?to add)\b",
    re.I,
)
SCORE_CUE = re.compile(r"\b(score[sd]?|total|points?|out of)\b", re.I)
NUMBER = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\b")
TOTAL_PATTERNS = [
    re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*(?:points?\s*)?(?:out of|/)\s*(\d{1,3})\b", re.I),
    re.compile(r"\b(?:total|score|scored|received|got)\b[^.?!]{0,40}?\b(\d{1,3}(?:\.\d+)?)\b", re.I),
]
WORD_NUMBERS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "twenty-one": 21, "twenty one": 21,
    "twenty-two": 22, "twenty two": 22, "twenty-three": 23, "twenty three": 23, "twenty-four": 24,
    "twenty four": 24, "twenty-five": 25, "twenty five": 25, "twenty-six": 26, "twenty six": 26,
    "twenty-seven": 27, "twenty seven": 27, "twenty-eight": 28, "twenty eight": 28, "twenty-nine": 29,
    "twenty nine": 29, "thirty": 30,
}
AWARD_CUE = re.compile(r"\b(winner|runner[- ]up|first place|second place|third place|award|audience choice|congratulations to)\b", re.I)
TECH_CUE = re.compile(r"\b(screen|share|sharing|see your|hear (?:you|me)|mute|unmute|audio|frozen|connection|slides? (?:up|showing))\b", re.I)
HEDGE = re.compile(
    r"\b(don'?t know|not sure|unclear|to be determined|tbd|still (?:working|figuring|evaluating)|haven'?t (?:yet|decided)|"
    r"we'?ll (?:see|find out)|can'?t (?:say|share|disclose)|depends|working on that|good question|get back to you)\b",
    re.I,
)
QUESTION_START = re.compile(
    r"^(?:so|and|ok(?:ay)?|um+|uh+|yeah|well|great|thanks?(?: you)?|i guess|my question is|quick question|"
    r"i have a question|a question for you)[,:\s]+",
    re.I,
)
NOTHING_TO_ADD = re.compile(
    r"\b(nothing (?:else |more |further )?to add|i (?:have )?nothing (?:else|more|further)|"
    r"(?:i )?agree with (?:everything|what|all)|covered (?:it|everything|my points)|ditto|same as (?:the others|everyone))\b",
    re.I,
)
POSITIVE = re.compile(
    r"\b(great|love[ds]?|impressive|impressed|strong|well done|excellent|liked|good job|compelling|nice|"
    r"exciting|fantastic|really good|clear|important problem|solid|congrat)\w*", re.I)
RECOMMEND = re.compile(
    r"\b(should|suggest|recommend|consider|need(?:s)? to|make sure|try to|focus|i'd|could|work on|tighten|shorten|"
    r"simplify|clarify|explain|would help|would like to see|want to see|encourage|be (?:more|clearer)|less)\b", re.I)

IMPERATIVE = re.compile(
    r"^(?:please\s+|so\s+|and\s+)?(connect|show|add|explain|simplify|shorten|clarify|focus|lead|cut|include|tell|use|make|"
    r"put|name|quantify|highlight|remove|reduce|think about|spend|bring|start|stop|drop|tighten|lay out|walk us)\b",
    re.I,
)
THEMES: dict[str, re.Pattern] = {
    "timeline_spend": re.compile(r"\b(timeline|milestones?|burn|spend(?:ing)?|use of (?:funds|proceeds)|runway|budget|cost[s]? (?:to|of)|capital plan)\b", re.I),
    "answer_length": re.compile(r"\b(shorten|shorter|concise|brief|briefer|long(?:er)? answers?|to the point|ramble|crisp|direct(?:ly)?) ?(?:answers?|responses?)?\b", re.I),
    "ip_competition": re.compile(r"\b(ip|patents?|intellectual property|competit\w*|moat|defensib\w*|protect\w*|barriers? to entry|differentiat\w*)\b", re.I),
    "investor_return": re.compile(r"\b(returns?|exit|liquidity|acquisition|acquirer|multiple|horizon|ipo|investor return)\b", re.I),
    "slides": re.compile(r"\b(slides?|deck|dense|text[- ]heavy|busy|font|visuals?|charts?)\b", re.I),
    "regulatory": re.compile(r"\b(fda|regulatory|approval|clinical|trial|barda|510\(?k\)?|ce mark|irb)\b", re.I),
    "market": re.compile(r"\b(market size|tam|go[- ]to[- ]market|customers?|pricing|sales|channel|distribution|reimbursement)\b", re.I),
    "traction": re.compile(r"\b(traction|revenue|pilots?|validation|loi|letters? of intent|users)\b", re.I),
    "team": re.compile(r"\b(team|hires?|hiring|advisors?|board|co-?founders?)\b", re.I),
    "financials": re.compile(r"\b(valuation|raise|raising|terms|cap table|financials?|round|safe|convertible)\b", re.I),
    "clarity": re.compile(r"\b(clear(?:er)?|clarity|jargon|simplif\w*|understand\w*|technical|lay ?person|non-?specialist)\b", re.I),
}
THEME_LABELS = {
    "timeline_spend": "Timeline and use of funds",
    "answer_length": "Answer length and directness",
    "ip_competition": "IP and competitive protection",
    "investor_return": "Investor return and liquidity",
    "slides": "Slide design and density",
    "regulatory": "Regulatory path",
    "market": "Market and go-to-market",
    "traction": "Traction and validation",
    "team": "Team",
    "financials": "Raise and terms",
    "clarity": "Clarity for non-specialists",
    "other": "Other",
}
CLAIM_CATEGORIES = {
    "regulatory": re.compile(r"\b(fda|cleared|approved|approval|510\(?k\)?|ce mark|breakthrough designation|barda)\b", re.I),
    "efficacy": re.compile(r"\b(efficacy|effective|cures?|reduces? mortality|clinically proven|outcomes?)\b", re.I),
    "ip": re.compile(r"\b(patents?(?: pending| granted| issued)?|exclusive licen[cs]e|ip portfolio)\b", re.I),
    "funding": re.compile(r"\b(grants?|non-?dilutive|awarded|sbir|sttr|contract with|term sheet|committed)\b", re.I),
    "projection": re.compile(r"\b(projected|projection|forecast|will reach|by 20\d\d|\$\d+(?:\.\d+)?\s?(?:m|million|b|billion))\b", re.I),
}


# ---------------------------------------------------------------------------
@dataclass
class UV:
    id: str
    idx: int
    t: float | None
    end: float | None
    raw: str
    speaker: str
    text: str
    is_gap: bool = False

    @property
    def words(self) -> int:
        return len(self.text.split())


@dataclass
class SpeakerInfo:
    label: str
    role: str  # host | judge | presenter | unknown
    person_id: str | None = None
    company_id: str | None = None
    certain: bool = False


@dataclass
class Boundary:
    start_idx: int | None
    end_idx: int | None
    confidence: float
    method: str
    note: str = ""


@dataclass
class Block:
    presenter: str
    pitch_start: int
    segments: dict[str, Boundary] = field(default_factory=dict)
    block_start: int = 0
    block_end: int = 0
    intro_text: str = ""
    names_mentioned: list[str] = field(default_factory=list)
    announced: dict | None = None
    notes: list[str] = field(default_factory=list)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.?!])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _host_speakers(utts: list[UV], info: dict[str, SpeakerInfo]) -> set[str]:
    hosts = {s for s, i in info.items() if i.role in ("host", "moderator")}
    cue_counts = Counter(u.speaker for u in utts if u.speaker and INTRO_CUE.search(u.text))
    if cue_counts:
        top, n = cue_counts.most_common(1)[0]
        if n >= 2 and info.get(top, SpeakerInfo(top, "unknown")).role not in ("judge", "presenter"):
            hosts.add(top)
    return hosts


def detect_presenters(utts: list[UV], info: dict[str, SpeakerInfo], hosts: set[str],
                      window_s: float = 15 * 60, min_words: int = 120, concentration: float = 0.7) -> list[str]:
    words_by: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for u in utts:
        if u.speaker and u.t is not None:
            words_by[u.speaker].append((u.t, u.words))
    presenters = []
    for sp, items in words_by.items():
        role = info.get(sp, SpeakerInfo(sp, "unknown")).role
        if sp in hosts or role == "judge":
            continue
        total = sum(w for _, w in items)
        if role == "presenter" and total >= 60:
            presenters.append(sp)
            continue
        if total < min_words:
            continue
        items.sort()
        best = 0
        j = 0
        acc = 0
        for i in range(len(items)):
            acc += items[i][1]
            while items[i][0] - items[j][0] > window_s:
                acc -= items[j][1]
                j += 1
            best = max(best, acc)
        if best / total >= concentration:
            presenters.append(sp)
    return presenters


def _first_substantive(utts: list[UV], speaker: str, start: int = 0, min_words: int = 25) -> int | None:
    for u in utts[start:]:
        if u.speaker == speaker and u.words >= min_words:
            return u.idx
    return None


def _parse_total(text: str) -> tuple[float | None, float | None]:
    t = normalize_punct(text).lower()
    for w, n in sorted(WORD_NUMBERS.items(), key=lambda kv: -len(kv[0])):
        t = re.sub(rf"\b{w}\b", str(n), t)
    m = TOTAL_PATTERNS[0].search(t)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = TOTAL_PATTERNS[1].search(t)
    if m:
        return float(m.group(1)), None
    return None, None


def _is_intro_for(u: UV, presenter: str, company_labels: list[str], adjacent: bool) -> bool:
    if not INTRO_CUE.search(u.text):
        return False
    if _mentions_any(u.text, [presenter] + company_labels):
        return True
    return adjacent and bool(STRONG_INTRO.search(u.text))


def segment_event(utts: list[UV], info: dict[str, SpeakerInfo],
                  company_labels: list[str] | None = None) -> tuple[list[Block], set[str], set[str]]:
    """Return blocks in presentation order, plus host and judge speaker sets.
    ``company_labels``: roster company names/aliases, used only to recognize introductions."""
    company_labels = company_labels or []
    hosts = _host_speakers(utts, info)
    presenters = detect_presenters(utts, info, hosts)
    starts = []
    for sp in presenters:
        i = _first_substantive(utts, sp, min_words=12)
        if i is not None:
            starts.append((i, sp))
    starts.sort()
    presenter_set = {sp for _, sp in starts}
    # Judges: mapped judges plus non-host, non-presenter speakers active in 2+ blocks.
    judges = {s for s, i in info.items() if i.role == "judge"}
    block_ranges = []
    for k, (i, sp) in enumerate(starts):
        nxt = starts[k + 1][0] if k + 1 < len(starts) else len(utts)
        block_ranges.append((i, nxt))
    activity: dict[str, set[int]] = defaultdict(set)
    for b, (i, nxt) in enumerate(block_ranges):
        for u in utts[i:nxt]:
            if u.speaker and u.speaker not in presenter_set and u.speaker not in hosts:
                activity[u.speaker].add(b)
    judges |= {s for s, bs in activity.items() if len(bs) >= 2 or len(block_ranges) == 1}

    # Pass 1: intro / opening update / pitch start for every presenter.
    pre: list[dict] = []
    for k, (first_idx, sp) in enumerate(starts):
        next_first = starts[k + 1][0] if k + 1 < len(starts) else len(utts)
        prev_first = starts[k - 1][0] if k > 0 else -1
        intro_idx = None
        for j in range(first_idx - 1, max(prev_first, first_idx - 7), -1):
            u = utts[j]
            if u.speaker in hosts and _is_intro_for(u, sp, company_labels, adjacent=(j == first_idx - 1)):
                intro_idx = j
                break
        opening = None
        pitch_start = first_idx
        if intro_idx is None:
            # Presenter gave a substantive opening update before the host's formal introduction.
            for j in range(first_idx + 1, min(next_first, first_idx + 12)):
                u = utts[j]
                if u.speaker in hosts and _is_intro_for(u, sp, company_labels, adjacent=True):
                    later = _first_substantive(utts, sp, j + 1, min_words=10)
                    early_words = sum(x.words for x in utts[first_idx:j] if x.speaker == sp)
                    if later is not None and later < next_first and early_words >= 25:
                        opening = Boundary(first_idx, j - 1, 0.6, "presenter update before host introduction")
                        intro_idx = j
                        pitch_start = later
                    break
        start = min(x for x in (intro_idx, opening.start_idx if opening else None, pitch_start) if x is not None)
        pre.append({"sp": sp, "intro": intro_idx, "opening": opening, "pitch_start": pitch_start, "start": start})

    blocks: list[Block] = []
    for k, p in enumerate(pre):
        sp, intro_idx, opening, pitch_start = p["sp"], p["intro"], p["opening"], p["pitch_start"]
        limit = pre[k + 1]["start"] if k + 1 < len(pre) else len(utts)
        b = Block(presenter=sp, pitch_start=pitch_start, block_start=p["start"], block_end=limit - 1)
        if opening:
            b.segments["opening_update"] = opening
        if intro_idx is not None:
            intro_end = pitch_start - 1 if opening is None else intro_idx
            b.segments["intro"] = Boundary(intro_idx, max(intro_idx, intro_end), 0.8, "host introduction cue")
            b.intro_text = " ".join(u.text for u in utts[intro_idx:max(intro_idx, intro_end) + 1])
        else:
            b.notes.append("No host introduction was found before this presentation.")

        # Q&A start
        qa_idx = None
        for j in range(pitch_start + 1, limit):
            u = utts[j]
            if u.speaker == sp or not u.speaker:
                continue
            if TECH_CUE.search(u.text) and "?" in u.text and not QA_CUE.search(u.text):
                continue  # screen-share / audio interruption during the pitch
            if (u.speaker in hosts and QA_CUE.search(u.text)) or (u.speaker in judges and "?" in u.text):
                qa_idx = j
                break
        pitch_end = None
        if qa_idx is not None:
            pitch_end = _last_by(utts, sp, pitch_start, qa_idx - 1)
            b.segments["qa"] = Boundary(qa_idx, None, 0.75, "first panel question")
        # Feedback start
        fb_idx = None
        method = ""
        search_start = (qa_idx or pitch_start) + 1
        for j in range(search_start, limit):
            u = utts[j]
            if u.speaker in hosts and FEEDBACK_HOST_CUE.search(u.text):
                fb_idx = j
                method = "host feedback cue"
                break
        if fb_idx is None:
            for j in range(search_start, limit):
                u = utts[j]
                if u.speaker in judges and FEEDBACK_JUDGE_CUE.search(u.text):
                    rest = sum(x.words for x in utts[j:limit] if x.speaker == sp)
                    if rest < 40:
                        fb_idx = j
                        method = "judge feedback phrasing"
                        break
        # Score announcement
        score_idx = None
        for j in range((fb_idx or search_start), limit):
            u = utts[j]
            if u.speaker in hosts and SCORE_CUE.search(u.text):
                val, den = _parse_total(u.text)
                if val is not None:
                    score_idx = j
                    b.announced = {"value": val, "denominator": den, "idx": j}
                    break
        if score_idx is not None:
            b.segments["score"] = Boundary(score_idx, score_idx, 0.8, "host announced a total")
        last_judge = None
        upper = (score_idx - 1) if score_idx is not None else limit - 1
        for j in range(upper, (fb_idx or search_start) - 1, -1):
            if utts[j].speaker in judges:
                last_judge = j
                break
        if fb_idx is not None:
            fb_end = last_judge if last_judge is not None and last_judge >= fb_idx else upper
            b.segments["feedback"] = Boundary(fb_idx, fb_end, 0.75 if method == "host feedback cue" else 0.6, method)
            if qa_idx is not None:
                b.segments["qa"].end_idx = fb_idx - 1
        elif qa_idx is not None:
            b.segments["qa"].end_idx = last_judge if last_judge is not None else upper
            b.segments["qa"].confidence = 0.5
            b.notes.append("No clear transition to panel feedback was found; Q&A end needs review.")
        if pitch_end is None:
            pitch_end = _last_by(utts, sp, pitch_start, upper)
            b.notes.append("No clear start of Q&A was found; pitch end needs review.")
        b.segments["pitch"] = Boundary(pitch_start, pitch_end, 0.8 if qa_idx is not None else 0.45,
                                       "presenter's main speech" + (" through last turn before first question" if qa_idx else ""))
        own = [u.text for u in utts[pitch_start:b.block_end + 1] if u.speaker == sp][:2]
        b.names_mentioned = _proper_names(b.intro_text + " " + " ".join(own))
        blocks.append(b)
    return blocks, hosts, judges


def find_awards(utts: list[UV], labels: list[str], hosts: set[str]) -> list[int]:
    out = []
    for u in utts:
        if u.speaker in hosts and AWARD_CUE.search(u.text) and _mentions_any(u.text, labels):
            out.append(u.idx)
    return out


def _last_by(utts: list[UV], sp: str, lo: int, hi: int) -> int:
    for j in range(hi, lo - 1, -1):
        if utts[j].speaker == sp:
            return j
    return lo


def _mentions_any(text: str, labels: list[str]) -> bool:
    t = norm_company(text)
    tn = norm_name(text)
    for lab in labels:
        if not lab:
            continue
        k = norm_company(lab)
        if k and len(k) >= 3 and k in t:
            return True
        first = norm_name(lab).split(" ")[0]
        if len(first) >= 3 and re.search(rf"\b{re.escape(first)}\b", tn):
            return True
    return False


STOP_CAPS = {"I", "We", "Our", "The", "Next", "Thank", "Thanks", "Welcome", "Please", "So", "And", "Up", "Let", "Let's",
             "Okay", "Ok", "Great", "Today", "Hi", "Hello", "Good", "Everyone", "Investors", "Judges", "Panel", "This",
             "That", "It", "Its", "You", "Your", "With", "From", "Take", "Away", "All", "Right", "Now", "Here", "Is",
             "A", "An", "Of", "Presenting", "Who", "Ready", "Whenever", "Over", "To", "Floor", "Yours", "First", "Morning",
             "Afternoon", "PitchForce", "EcoVG", "Ecosystem", "Venture", "Group", "CEO", "Founder", "Co-founder", "Q&A"}


def _proper_names(text: str) -> list[str]:
    """Capitalized word runs (candidate company/person names) from the intro, for matching proposals.
    Single capitalized words at the start of a sentence are ignored (ordinary sentence capitalization)."""
    out = []
    for m in re.finditer(r"\b([A-Z][\w&'’\-]*(?:\s+[A-Z][\w&'’\-]*){0,3})", text):
        words = [w for w in m.group(1).split() if w not in STOP_CAPS and w.upper() != w]
        if not words:
            continue
        before = text[: m.start()].rstrip()
        at_sentence_start = not before or before[-1] in ".?!:"
        lead_dropped = m.group(1).split()[0] in STOP_CAPS
        if len(words) == 1 and at_sentence_start and not lead_dropped:
            continue
        name = " ".join(words)
        if name not in out and len(name) > 2:
            out.append(name)
    return out


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------
@dataclass
class QItem:
    seq: int
    asker: str
    asker_role: str
    utt_idx: int
    excerpt: str
    paraphrase: str
    parts: list[str]
    is_followup: bool
    answer_idxs: list[int]
    answer_excerpt: str
    answered: str
    unresolved: str
    completeness: str
    importance: int = 0


def _clean_question(s: str) -> str:
    s = QUESTION_START.sub("", s.strip())
    s = re.sub(r"\b(um+|uh+|you know|kind of|sort of)\b,?\s*", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:1].upper() + s[1:] if s else s


MULTIPART = re.compile(
    r",?\s+(?:and|or)\s+(?=(?:who|what|when|where|why|how|which|do|does|did|is|are|can|will|would|have|has)\b)"
    r"|,\s+(?=(?:do|does|did|is|are|can|will|would|have|has|who|what|when|where|why|how)\b)",
    re.I,
)


def _split_multipart(q: str) -> list[str]:
    """'Which model will they accept, do you have access, and who are competitors?' -> three parts."""
    parts = [p.strip(" ,") for p in MULTIPART.split(q) if p and p.strip(" ,")]
    if len(parts) < 2 or any(len(p.split()) < 3 for p in parts):
        return [q]
    return [p if p.endswith("?") else p + "?" for p in parts]


def extract_questions(utts: list[UV], qa: Boundary, presenter: str, hosts: set[str], judges: set[str]) -> list[QItem]:
    if qa.start_idx is None:
        return []
    end = qa.end_idx if qa.end_idx is not None else qa.start_idx
    rng = [u for u in utts[qa.start_idx:end + 1]]
    items: list[QItem] = []
    last_asker = None
    seen_answer_since = False
    for k, u in enumerate(rng):
        if u.speaker == presenter or not u.speaker:
            if items:
                seen_answer_since = True
            continue
        qsents = [s for s in _sentences(u.text) if s.endswith("?")]
        trailing = False
        if not qsents:
            # Questions that trail off (gap, or cut off mid-sentence) are kept as incomplete.
            if u.speaker in judges and (u.is_gap or not re.search(r"[.!?\"”)]\s*$", u.text)):
                qsents = [u.text]
                trailing = True
            else:
                continue
        if len(qsents) == 1 and not trailing:
            parts = _split_multipart(qsents[0])
            if len(parts) > 1:
                qsents = parts
        role = "moderator" if u.speaker in hosts else ("investor" if u.speaker in judges else "unknown")
        # Moderator logistics ("Any other questions?") are recorded but marked as moderator.
        ans_idx = []
        for v in rng[k + 1:]:
            if v.speaker == presenter:
                ans_idx.append(v.idx)
            elif v.speaker and ans_idx:
                break
            elif v.speaker and not ans_idx and "?" in v.text:
                break
        answer_text = " ".join(utts[i].text for i in ans_idx)
        gap_q = u.is_gap
        gap_a = any(utts[i].is_gap for i in ans_idx)
        completeness = "gap" if (gap_q or gap_a) else ("incomplete" if (trailing or not ans_idx) else "complete")
        hedge = HEDGE.search(answer_text)
        if trailing:
            unresolved = "The question was cut off in the transcript; the full question is not recorded."
            answered = "Partly answered." if ans_idx else ""
        elif not ans_idx:
            unresolved = "No answer from the presenter was recorded before the next speaker."
            answered = ""
        elif gap_a:
            unresolved = "Part of the answer is missing from the transcript (gap); not reconstructed."
            answered = "Partially recorded answer."
        elif hedge:
            unresolved = f"The presenter indicated this is not yet settled (“{hedge.group(0)}”)."
            answered = "Partly answered."
        else:
            unresolved = ""
            answered = "Answered."
        if ans_idx and not gap_a:
            first = _sentences(answer_text)[0] if _sentences(answer_text) else answer_text
            words = first.split()
            snippet = " ".join(words[:28]) + ("…" if len(words) > 28 else "")
            answered = (answered.rstrip(".") + ": " if answered else "Answered: ") + f"“{snippet}”"
        excerpt = u.text
        items.append(QItem(
            seq=len(items) + 1, asker=u.speaker, asker_role=role, utt_idx=u.idx, excerpt=excerpt,
            paraphrase=_clean_question(qsents[0]) if len(qsents) == 1 else " / ".join(_clean_question(s) for s in qsents),
            parts=[_clean_question(s) for s in qsents] if len(qsents) > 1 else [],
            is_followup=(last_asker == u.speaker and seen_answer_since and role == "investor"),
            answer_idxs=ans_idx, answer_excerpt=answer_text[:1200], answered=answered, unresolved=unresolved,
            completeness=completeness,
        ))
        last_asker = u.speaker
        seen_answer_since = False
    # importance: investor questions with follow-ups or unresolved points rank higher
    for q in items:
        q.importance = (2 if q.asker_role == "investor" else 0) + (1 if q.unresolved else 0) + \
            sum(1 for o in items if o.is_followup and o.asker == q.asker and o.seq == q.seq + 1)
    return items


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------
@dataclass
class FItem:
    speaker: str
    utt_idx: int | None
    kind: str
    point: str
    theme: str
    excerpt: str


def theme_of(text: str) -> str:
    best, n = "other", 0
    for k, pat in THEMES.items():
        c = len(pat.findall(text))
        if c > n:
            best, n = k, c
    return best


def extract_feedback(utts: list[UV], fb: Boundary, presenter: str, hosts: set[str], judges: set[str],
                     present_judges: list[str]) -> list[FItem]:
    if fb.start_idx is None:
        return []
    end = fb.end_idx if fb.end_idx is not None else fb.start_idx
    items: list[FItem] = []
    spoke = set()
    for u in utts[fb.start_idx:end + 1]:
        if not u.speaker or u.speaker == presenter or u.speaker in hosts:
            continue
        spoke.add(u.speaker)
        if NOTHING_TO_ADD.search(u.text) and u.words < 30:
            items.append(FItem(u.speaker, u.idx, "nothing_to_add", "Nothing to add beyond the other panelists' comments.", "other", u.text))
            continue
        for s in _sentences(u.text):
            pos = bool(POSITIVE.search(s))
            rec = bool(RECOMMEND.search(s)) or bool(IMPERATIVE.match(s))
            if len(s.split()) < 4 and not (pos or rec):
                continue
            if rec:
                kind = "recommendation"
            elif pos:
                kind = "positive"
            else:
                kind = "comment"
            items.append(FItem(u.speaker, u.idx, kind, s, theme_of(s), s))
    for j in present_judges:
        if j not in spoke:
            items.append(FItem(j, None, "silent", "No feedback from this panelist was recorded in the transcript for this company.", "other", ""))
    return items


def founder_claims(utts: list[UV], pitch: Boundary, presenter: str) -> list[tuple[int, str, str]]:
    out = []
    if pitch.start_idx is None:
        return out
    for u in utts[pitch.start_idx:(pitch.end_idx or pitch.start_idx) + 1]:
        if u.speaker != presenter:
            continue
        for s in _sentences(u.text):
            for cat, pat in CLAIM_CATEGORIES.items():
                if pat.search(s):
                    out.append((u.idx, cat, s))
                    break
    return out
