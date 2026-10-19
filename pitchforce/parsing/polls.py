"""Investment-interest poll parsing.

Handles:
- Zoom poll report CSVs that contain several sections (Overview / Launch History / Poll Details ...)
- ordinary CSV and multi-sheet XLSX exports
- quoted multiline fields, multi-select answers, blank/trailing columns, Unicode punctuation

The parser only *reads*; event filtering, company assignment, deduplication, and identity checks
are done in ``services/interest.py`` with the event context.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from ..util import norm_name, normalize_punct
from .tabular import Sheet, get, header_key, map_columns

SECTION_NAMES = {
    "overview": "overview",
    "launch history": "launch_history",
    "launches": "launch_history",
    "poll details": "poll_details",
    "poll detail": "poll_details",
    "question details": "poll_details",
    "responses": "poll_details",
    "poll report": "report_title",
    "report generated": "report_title",
}

LAUNCH_ALIASES = {
    "index": ["#", "launch", "launch #", "launch number", "no"],
    "meeting_id": ["meeting webinar id", "webinar id", "meeting id", "meeting webinar", "id"],
    "topic": ["topic", "meeting topic", "webinar topic"],
    "poll_name": ["poll name", "poll title", "title", "poll"],
    "start": ["launch start time", "start time", "launched at", "launch time", "start"],
    "end": ["launch end time", "end time", "end", "closed at"],
}

DETAIL_ALIASES = {
    "rid": ["#", "response id", "id", "no"],
    "name": ["user name", "name", "participant", "participant name", "attendee", "full name", "respondent"],
    "email": ["email address", "user email", "email", "e mail"],
    "submitted": ["submitted date and time", "submitted date time", "submitted at", "submission time", "date submitted", "submitted", "timestamp", "time"],
    "meeting_id": ["meeting webinar id", "webinar id", "meeting id"],
    "launch": ["launch", "launch #", "launch number", "launch start time"],
    "poll_name": ["poll name", "poll title"],
    "first": ["first name"],
    "last": ["last name"],
    "company": ["company", "presenting company", "startup", "company presenting"],
    "affiliation": ["affiliation", "organization", "organisation", "firm", "your company", "your organization"],
    "phone": ["phone", "phone number", "mobile"],
}
KNOWN_DETAIL_KEYS = {a for v in DETAIL_ALIASES.values() for a in v}

DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p",
    "%b %d, %Y %H:%M:%S", "%b %d, %Y %I:%M:%S %p", "%b %d, %Y %I:%M %p", "%B %d, %Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
]
TZ_HINT = re.compile(r"\b(GMT|UTC)\s*([+-]\d{1,2}(?::?\d{2})?)?|\b(America|Europe|Asia|Australia|Africa|Pacific)/[A-Za-z_]+\b|\b(PDT|PST|EDT|EST|CDT|CST|MDT|MST)\b")


def parse_datetime(raw: str) -> datetime | None:
    s = normalize_punct(raw or "").strip()
    s = TZ_HINT.sub("", s).strip().rstrip(",").strip()
    s = re.sub(r"\s+", " ", s)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def normalize_meeting_id(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


@dataclass
class RawLaunch:
    index: int
    meeting_id_raw: str
    topic: str
    poll_name: str
    start_raw: str
    end_raw: str
    ref: str


@dataclass
class RawResponse:
    rid: str
    name: str
    email: str
    submitted_raw: str
    meeting_id_raw: str
    launch_raw: str
    poll_name: str
    question: str
    answer: str
    ref: str
    original: dict[str, str]
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class PollParse:
    layout: str = ""
    overview: dict[str, str] = field(default_factory=dict)
    launches: list[RawLaunch] = field(default_factory=list)
    responses: list[RawResponse] = field(default_factory=list)
    timezone_hint: str = ""
    warnings: list[str] = field(default_factory=list)


def _section_of(row: list[str]) -> str | None:
    cells = [c.strip() for c in row if c and c.strip()]
    if not cells:
        return None
    first = header_key(cells[0]).rstrip(":")
    if first in SECTION_NAMES and (len(cells) == 1 or first in ("report generated", "poll report")):
        return SECTION_NAMES[first]
    return None


def _split_sections(sheet: Sheet) -> list[tuple[str, int, int]]:
    """Return [(section, start_row, end_row_exclusive)]."""
    marks = []
    for i, row in enumerate(sheet.rows):
        sec = _section_of(row)
        if sec:
            marks.append((sec, i))
    if not marks:
        return [("table", 0, len(sheet.rows))]
    out = []
    if marks[0][1] > 0:
        out.append(("preamble", 0, marks[0][1]))
    for k, (sec, i) in enumerate(marks):
        end = marks[k + 1][1] if k + 1 < len(marks) else len(sheet.rows)
        out.append((sec, i + 1, end))
    return out


def _first_header(sheet: Sheet, start: int, end: int, aliases: dict, min_hits: int):
    for i in range(start, end):
        m = map_columns(sheet.rows[i], aliases)
        if len(m) >= min_hits:
            return i, m
    return None, {}


def _parse_launches(sheet: Sheet, start: int, end: int, out: PollParse) -> None:
    hi, m = _first_header(sheet, start, end, LAUNCH_ALIASES, 2)
    if hi is None:
        out.warnings.append("Launch History section found but its header row was not recognized.")
        return
    for ri in range(hi + 1, end):
        row = sheet.rows[ri]
        if not any(c.strip() for c in row):
            continue
        idx_raw = get(row, m, "index")
        try:
            idx = int(re.sub(r"\D", "", idx_raw) or len(out.launches) + 1)
        except ValueError:
            idx = len(out.launches) + 1
        out.launches.append(
            RawLaunch(
                index=idx,
                meeting_id_raw=get(row, m, "meeting_id"),
                topic=get(row, m, "topic"),
                poll_name=get(row, m, "poll_name"),
                start_raw=get(row, m, "start"),
                end_raw=get(row, m, "end"),
                ref=f"Launch History, {sheet.cell_ref(ri, 0)}",
            )
        )


def _parse_details(sheet: Sheet, start: int, end: int, out: PollParse) -> None:
    hi, m = _first_header(sheet, start, end, DETAIL_ALIASES, 2)
    if hi is None:
        out.warnings.append(f"Response table header not recognized in {sheet.name}.")
        return
    header = sheet.rows[hi]
    used = set(m.values())
    question_cols = [j for j, h in enumerate(header) if h.strip() and j not in used and header_key(h) not in KNOWN_DETAIL_KEYS]
    # A layout with "Question" / "Answer" columns:
    qa = map_columns(header, {"question": ["question", "poll question"], "answer": ["answer", "response", "answers", "selection"]})
    for ri in range(hi + 1, end):
        row = sheet.rows[ri]
        if not any(c.strip() for c in row):
            continue
        if _section_of(row):
            break
        name = get(row, m, "name") or " ".join(x for x in (get(row, m, "first"), get(row, m, "last")) if x)
        original = {(header[j] or f"(blank column {j+1})"): (row[j] if j < len(row) else "") for j in range(len(header)) if header[j].strip() or (j < len(row) and row[j].strip())}
        extra = {k: get(row, m, k) for k in ("company", "phone") if k in m and get(row, m, k)}
        if "affiliation" in m and get(row, m, "affiliation"):
            extra["company_affiliation"] = get(row, m, "affiliation")
        base = dict(
            rid=get(row, m, "rid"), name=name, email=get(row, m, "email"), submitted_raw=get(row, m, "submitted"),
            meeting_id_raw=get(row, m, "meeting_id"), launch_raw=get(row, m, "launch"), poll_name=get(row, m, "poll_name"),
            ref=f"{sheet.name}: {sheet.cell_ref(ri, 0)}" + (f" (ends line {sheet.line_refs[ri]})" if sheet.line_refs else ""),
            original=original, extra=extra,
        )
        if "answer" in qa:
            question = get(row, qa, "question") if "question" in qa else header[qa["answer"]].strip()
            out.responses.append(RawResponse(question=question, answer=get(row, qa, "answer"), **base))
            continue
        qcols = [j for j in question_cols if j not in qa.values()]
        answered = [(header[j].strip(), row[j]) for j in qcols if j < len(row) and row[j].strip()]
        if not answered:
            # Keep the response record: an empty answer is still a submission to preserve.
            q = header[qcols[0]].strip() if qcols else ""
            out.responses.append(RawResponse(question=q, answer="", **base))
            continue
        for q, a in answered:
            out.responses.append(RawResponse(question=q, answer=a, **base))


def parse_poll_sheets(sheets: list[Sheet]) -> PollParse:
    out = PollParse()
    for sheet in sheets:
        joined = " ".join(" ".join(r) for r in sheet.rows[:15])
        tz = TZ_HINT.search(joined)
        if tz and not out.timezone_hint:
            out.timezone_hint = tz.group(0)
        sections = _split_sections(sheet)
        if len(sections) > 1 or sections[0][0] != "table":
            out.layout = "zoom_multi_section"
        else:
            out.layout = out.layout or ("xlsx" if sheet.kind == "xlsx" else "csv")
        for sec, s, e in sections:
            if sec == "launch_history":
                _parse_launches(sheet, s, e, out)
            elif sec in ("poll_details", "table"):
                _parse_details(sheet, s, e, out)
            elif sec in ("overview", "preamble", "report_title"):
                rows = sheet.rows[s:e]
                if len(rows) >= 2:
                    hdr = rows[0]
                    for r in rows[1:2]:
                        for j, h in enumerate(hdr):
                            if h.strip() and j < len(r):
                                out.overview[h.strip()] = r[j]
                    # Overview header names that look like launches (Topic/Webinar ID/Start) form a single launch.
                    m = map_columns(hdr, LAUNCH_ALIASES)
                    if "meeting_id" in m and not out.launches:
                        out.overview["_meeting_id"] = get(rows[1], m, "meeting_id")
    if not out.responses:
        out.warnings.append("No poll responses found.")
    return out


# ---------------------------------------------------------------------------
# Choice splitting and categorization
# ---------------------------------------------------------------------------
DEFAULT_RULES: list[tuple[str, str]] = [
    (r"\bnot\s+interested\b|\bno\s+interest\b|\bpass\b", "not_interested"),
    (r"due\s+diligence|\bdiligence\b", "due_diligence"),
    (r"\bmore\s+info|\bmore\s+information|\blearn\s+more\b|\bsend\s+(?:me\s+)?(?:the\s+)?(?:deck|info)", "more_info"),
    (r"interested\s+in\s+invest|\binvest(?:ing|ment)?\b", "invest"),
    (r"\bmeet(?:ing)?\b|\bschedule\b|\bcall\b|\bconnect\b", "meeting"),
    (r"free\s+resource|\bresource", "free_resource"),
    (r"\bintroduc|\bintro\b|\bintros\b", "introduction"),
    (r"\bmentor|\badvis(?:e|or|ing)\b", "mentoring"),
]


def split_choices(answer: str) -> list[str]:
    if not answer or not answer.strip():
        return []
    parts = re.split(r"\s*(?:\r?\n|;|\|)\s*", answer.strip())
    return [p.strip() for p in parts if p.strip()]


def categorize(choice: str, rules: list[tuple[str, str]] | None = None) -> str:
    text = norm_name(normalize_punct(choice))
    for pattern, cat in rules or DEFAULT_RULES:
        if re.search(pattern, text, re.I):
            return cat
    return "unknown"


POSITIVE = {"invest", "meeting", "due_diligence", "free_resource", "introduction", "mentoring"}


def is_contradictory(categories: set[str]) -> bool:
    return "not_interested" in categories and bool(categories & (POSITIVE | {"more_info"}))
