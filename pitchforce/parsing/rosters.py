"""Company/presenter rosters, judge rosters, and judge score sheets."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .tabular import Sheet, find_header, get, header_key, map_columns
from .transcript import parse_zoom_chat

COMPANY_ALIASES = {
    "company": ["company", "company name", "presenting company", "startup", "organization", "organisation", "business"],
    "presenter": ["presenter", "presenter name", "founder", "speaker", "name", "contact", "contact name", "ceo"],
    "first": ["first name", "first"],
    "last": ["last name", "last", "surname"],
    "email": ["email", "email address", "e mail", "presenter email", "contact email"],
    "phone": ["phone", "phone number", "mobile", "cell"],
    "title": ["title", "role", "position", "job title"],
    "website": ["website", "url", "web", "company website"],
    "slot": ["slot", "order", "presentation order", "pitch order", "#", "no", "number"],
}

JUDGE_ALIASES = {
    "name": ["name", "judge", "judge name", "investor", "investor name", "panelist", "full name"],
    "first": ["first name", "first"],
    "last": ["last name", "last", "surname"],
    "affiliation": ["affiliation", "firm", "organization", "organisation", "company", "fund"],
    "email": ["email", "email address", "e mail"],
    "phone": ["phone", "phone number", "mobile", "cell"],
    "title": ["title", "role", "position"],
    "attendance": ["attendance", "attended", "status", "present", "attending"],
}


@dataclass
class RosterRow:
    ref: str
    row_number: int
    values: dict[str, str]
    original: dict[str, str]


@dataclass
class RosterParse:
    rows: list[RosterRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sheet: str = ""


def _parse_roster(sheets: list[Sheet], aliases: dict, required: str) -> RosterParse:
    out = RosterParse()
    for sheet in sheets:
        hi, mapping = find_header(sheet, aliases, min_hits=1)
        if hi is None or (required not in mapping and not ({"first", "last"} <= mapping.keys())):
            continue
        out.sheet = sheet.name
        header = sheet.rows[hi]
        for ri in range(hi + 1, len(sheet.rows)):
            row = sheet.rows[ri]
            vals = {k: get(row, mapping, k) for k in mapping}
            if not vals.get(required) and "first" in vals:
                vals[required if required != "company" else "presenter"] = " ".join(
                    x for x in (vals.get("first", ""), vals.get("last", "")) if x
                )
            if required == "company" and "presenter" not in vals and ("first" in vals or "last" in vals):
                vals["presenter"] = " ".join(x for x in (vals.get("first", ""), vals.get("last", "")) if x)
            if not any(v for v in vals.values()):
                continue
            original = {header[j] or f"col{j+1}": (row[j] if j < len(row) else "") for j in range(len(header))}
            out.rows.append(RosterRow(ref=sheet.cell_ref(ri, 0).split(",")[0] if sheet.kind == "csv" else f"{sheet.name}!row {sheet.row_refs[ri]}",
                                      row_number=sheet.row_refs[ri], values=vals, original=original))
        if out.rows:
            break
    if not out.rows:
        out.warnings.append(f"No rows found with a recognizable '{required}' column.")
    return out


def parse_company_roster(sheets: list[Sheet]) -> RosterParse:
    return _parse_roster(sheets, COMPANY_ALIASES, "company")


def parse_judge_roster(sheets: list[Sheet]) -> RosterParse:
    return _parse_roster(sheets, JUDGE_ALIASES, "name")


def attendance_status(raw: str) -> str:
    k = header_key(raw)
    if not k:
        return "unknown"
    if k in {"absent", "no", "n", "did not attend", "not present", "no show", "noshow", "absent confirmed"}:
        return "absent_confirmed"
    if k in {"abstain", "abstained", "recused", "did not score", "non scoring", "nonscoring"}:
        return "abstained_confirmed"
    if k in {"present", "yes", "y", "attended", "x", "confirmed", "attending"}:
        return "present"
    return "unknown"


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------
@dataclass
class RawScore:
    company_label: str
    judge_label: str
    value_raw: str
    ref: str
    source_kind: str = "file"


@dataclass
class FileTotal:
    company_label: str
    value_raw: str
    ref: str


@dataclass
class ScoreParse:
    scores: list[RawScore] = field(default_factory=list)
    totals: list[FileTotal] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    layout: str = ""


LONG_ALIASES = {
    "company": ["company", "company name", "presenting company", "startup", "presenter company"],
    "judge": ["judge", "judge name", "investor", "panelist", "scorer", "evaluator", "name"],
    "score": ["score", "rating", "points", "judge score", "value"],
}
TOTAL_HEADERS = {"total", "sum", "total score", "overall", "announced total"}
NON_JUDGE_HEADERS = TOTAL_HEADERS | {"average", "avg", "mean", "rank", "notes", "comments", "slot", "order", "presenter", "email"}


def parse_scores_table(sheets: list[Sheet]) -> ScoreParse:
    out = ScoreParse()
    for sheet in sheets:
        hi, mapping = find_header(sheet, LONG_ALIASES, min_hits=3)
        if hi is not None:
            out.layout = "long"
            for ri in range(hi + 1, len(sheet.rows)):
                row = sheet.rows[ri]
                c, j, v = get(row, mapping, "company"), get(row, mapping, "judge"), get(row, mapping, "score")
                if not (c or j or v):
                    continue
                out.scores.append(RawScore(c, j, v, sheet.cell_ref(ri, mapping["score"])))
            continue
        # Wide layout: first column company, one column per judge.
        hi, mapping = find_header(sheet, {"company": LONG_ALIASES["company"]}, min_hits=1)
        if hi is None:
            continue
        out.layout = "wide"
        header = sheet.rows[hi]
        ccol = mapping["company"]
        for ri in range(hi + 1, len(sheet.rows)):
            row = sheet.rows[ri]
            company = row[ccol].strip() if ccol < len(row) else ""
            if not company:
                continue
            for j, h in enumerate(header):
                if j == ccol or not h.strip():
                    continue
                hk = header_key(h)
                val = row[j].strip() if j < len(row) else ""
                if hk in TOTAL_HEADERS:
                    if val:
                        out.totals.append(FileTotal(company, val, sheet.cell_ref(ri, j)))
                    continue
                if hk in NON_JUDGE_HEADERS:
                    continue
                # Blank cell = no score in source (missing data, NOT absence).
                out.scores.append(RawScore(company, h.strip(), val, sheet.cell_ref(ri, j)))
    if not out.scores and not out.totals:
        out.warnings.append("No judge scores recognized. Expected columns Company/Judge/Score, or one column per judge.")
    return out


CHAT_SCORE = re.compile(
    r"^(?P<company>[A-Za-z][\w .&'’\-]{1,60}?)\s*[:\-–=]\s*(?P<score>\d(?:[.,]\d+)?)\s*(?:/\s*5)?\s*$"
)


def parse_chat_scores(content: bytes) -> ScoreParse:
    """Scores typed into Zoom chat like 'CaptionLens: 4' or 'CaptionLens - 3.5'."""
    out = ScoreParse(layout="chat")
    for msg in parse_zoom_chat(content):
        m = CHAT_SCORE.match(msg.text.strip())
        if m:
            out.scores.append(
                RawScore(m.group("company").strip(), msg.sender, m.group("score").replace(",", "."), f"chat line {msg.line} ({msg.ts_raw})", "chat")
            )
    if not out.scores:
        out.warnings.append("No 'Company: score' messages found in the chat export.")
    return out


def parse_score_value(raw: str, lo: float, hi: float, allow_fractional: bool) -> tuple[float | None, str, str]:
    """Return (value, status, reason)."""
    s = (raw or "").strip().replace(",", ".")
    if not s:
        return None, "missing", "No score in source"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:/\s*\d+)?", s)
    if not m:
        return None, "invalid", f"Not a number: {raw!r}"
    v = float(m.group(1))
    if v < lo or v > hi:
        return v, "out_of_range", f"{v:g} is outside the {lo:g}–{hi:g} scale"
    if not allow_fractional and not v.is_integer():
        return v, "invalid", "Fractional scores are not allowed by this event's rules"
    return v, "valid", ""


_ = map_columns  # re-exported for callers
