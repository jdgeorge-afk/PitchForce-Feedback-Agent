"""EcoVG best-practices guidance: versioned, operator-editable, adapted per event.

The initial version is built from the guidance extracted in the project brief, because the original
DOCX/ZIP files were not available in the build environment. Operators can upload the originals
(Settings -> Best practices); the importer reads DOCX/PDF/ZIP safely, prefers current copies over
anything in an "Old"/archive folder, detects duplicates, and flags the known contradictory wording.
These documents are reference content only - nothing in them authorizes contacting anyone.
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import BestPracticeVersion, Event
from ..parsing.documents import ArchiveError, docx_text, pdf_text, safe_extract_zip

DEFAULT_CONTENT = {
    "principles": [
        {"id": "clarity", "text": "Keep the pitch clear, concise, and understandable to non-specialists.", "themes": ["clarity", "slides"]},
        {"id": "direct_answers", "text": "Answer questions directly and briefly; lead with the answer, then stop.", "themes": ["answer_length"]},
        {"id": "listen", "text": "Listen to feedback and look for the patterns across panelists.", "themes": []},
        {"id": "time", "text": "Respect the allotted pitch time.", "themes": []},
        {"id": "consistency", "text": "Keep valuation, raise, traction, deal terms, and other facts consistent across your deck, company profile, pitch, and follow-up conversations.", "themes": ["financials", "traction"]},
        {"id": "next_step", "text": "Use each expression of interest to set a specific next step, such as a scheduled call.", "themes": []},
        {"id": "qualified", "text": "Aim for qualified follow-up meetings rather than simply collecting contacts.", "themes": []},
        {"id": "prompt", "text": "Send requested information promptly.", "themes": []},
        {"id": "beyond_capital", "text": "Consider introductions, mentoring, partnerships, and resources as well as investment.", "themes": []},
        {"id": "ownership", "text": "You own your investor relationships and follow-up. EcoVG facilitates events, introductions, and feedback, but does not guarantee responses, meetings, or investments.", "themes": []},
    ],
    "format_guidance": {
        "virtual": "After a virtual event, suggest a short follow-up video call rather than an in-person meeting.",
        "in_person": "At in-person events, use booth time and networking to book specific follow-up meetings.",
        "hybrid": "Offer both a video call and an in-person option, depending on how each contact attended.",
    },
    "cadence_guidance": {
        "text": "EcoVG's detailed guide suggests a multichannel follow-up cadence (about three emails, three calls, three texts, and one LinkedIn message over roughly nine days, with buffer). Treat this as optional guidance and adapt it to each contact.",
        "enabled_in_report": False,
    },
    "report_wording": {
        "best_practices_intro": "A few practices from EcoVG's presenting-company guidance that apply to your follow-up:",
        "interest_reference": "Investor and audience follow-up details are in the attached spreadsheet.",
        "no_guarantee": "EcoVG facilitates events, introductions, and feedback; responses, meetings, and investments are not guaranteed.",
    },
}

DEFAULT_ISSUES = [
    {
        "id": "responsibility-contradiction",
        "severity": "high",
        "text": "The detailed cheat sheet's responsibility section lists items under “We request the company to” that read as EcoVG following "
                "up “on your behalf” and guaranteeing investment outcomes, while the surrounding text states that founders own follow-up and "
                "that nothing is guaranteed. Reports use the consistent principle: founders manage follow-up; EcoVG does not guarantee outcomes.",
        "status": "open",
    },
    {
        "id": "event-specific-times",
        "severity": "medium",
        "text": "Source documents mention event-specific times (for example 10 a.m.–noon, Friday noon, 4–6 p.m.). These are not copied into reports for other events.",
        "status": "open",
    },
    {
        "id": "dd-template",
        "severity": "medium",
        "text": "An email template in the source says due diligence is complete. Reports never state that due diligence is complete unless an operator confirms it.",
        "status": "open",
    },
]

SOURCE_NOTE = ("Initial version built from guidance extracted in the project brief (COMPANY EVENT CHEAT SHEET- ONE PAGER.docx, "
               "PRESENTING COMPANY CHEAT SHEET- Latest ST.docx, 6. Company Cheat Sheet- Ready to rpint.zip). The original files "
               "were not available to the build environment; upload them to create a reviewed version.")

CONTRADICTION_PATTERNS = [
    (re.compile(r"on (?:your|the company'?s) behalf", re.I), "Mentions follow-up “on your behalf”"),
    (re.compile(r"guarantee[sd]?\s+(?:an?\s+)?(?:investment|funding|outcome|meeting)", re.I), "Mentions guaranteed outcomes"),
    (re.compile(r"due diligence (?:is|has been) complete", re.I), "States due diligence is complete"),
    (re.compile(r"\b(?:10\s?a\.?m\.?\s?[–-]\s?noon|friday noon|4\s?[–-]\s?6\s?p\.?m\.?)\b", re.I), "Contains event-specific times"),
]


def ensure_default(db: Session) -> BestPracticeVersion:
    v = db.scalar(select(BestPracticeVersion).where(BestPracticeVersion.status == "approved").order_by(BestPracticeVersion.version.desc()))
    if v:
        return v
    v = BestPracticeVersion(version=1, status="approved", content=DEFAULT_CONTENT, issues=DEFAULT_ISSUES,
                            source_docs=[{"name": "Project brief extraction", "note": SOURCE_NOTE}], notes=SOURCE_NOTE)
    db.add(v)
    db.flush()
    return v


def current(db: Session) -> BestPracticeVersion:
    return ensure_default(db)


def new_version(db: Session, content: dict, notes: str, user_id: str | None, approve: bool = True,
                source_docs: list | None = None, issues: list | None = None) -> BestPracticeVersion:
    cur = current(db)
    n = (db.scalar(select(func.max(BestPracticeVersion.version))) or 0) + 1
    v = BestPracticeVersion(version=n, status="approved" if approve else "draft", content=content, notes=notes,
                            created_by=user_id, source_docs=source_docs or cur.source_docs,
                            issues=issues if issues is not None else cur.issues)
    if approve:
        for old in db.scalars(select(BestPracticeVersion).where(BestPracticeVersion.status == "approved")):
            old.status = "archived"
    db.add(v)
    db.flush()
    return v


def import_documents(filename: str, data: bytes) -> dict:
    """Read uploaded guidance documents. Returns texts, duplicates, archived copies, and detected issues."""
    docs = []
    dupes = []
    skipped = []
    if filename.lower().endswith(".zip"):
        try:
            res = safe_extract_zip(data, allowed_ext={".docx", ".pdf", ".txt", ".md"})
        except ArchiveError as e:
            return {"error": str(e)}
        dupes = [{"path": p, "duplicate_of": o} for p, o in res.duplicates]
        skipped = res.skipped
        dup_paths = {p for p, _ in res.duplicates}
        for m in res.members:
            docs.append({"name": m.path, "sha256": m.sha256, "archived": m.archived, "duplicate": m.path in dup_paths,
                         "text": _text(m.path, m.data)})
    else:
        docs.append({"name": filename, "sha256": "", "archived": False, "duplicate": False, "text": _text(filename, data)})
    preferred = [d for d in docs if not d["archived"] and not d["duplicate"]]
    issues = []
    for d in preferred:
        for pat, label in CONTRADICTION_PATTERNS:
            for m in pat.finditer(d["text"]):
                start = max(0, m.start() - 80)
                issues.append({"doc": d["name"], "issue": label, "excerpt": d["text"][start:m.end() + 80].replace("\n", " ")})
    return {
        "documents": [{k: v for k, v in d.items() if k != "text"} | {"chars": len(d["text"])} for d in docs],
        "preferred": [d["name"] for d in preferred],
        "duplicates": dupes,
        "skipped": skipped,
        "issues": issues,
        "texts": {d["name"]: d["text"][:20000] for d in preferred},
        "imported_at": datetime.utcnow().isoformat(timespec="seconds"),
    }


def _text(name: str, data: bytes) -> str:
    n = name.lower()
    try:
        if n.endswith(".docx"):
            return docx_text(data)
        if n.endswith(".pdf"):
            return "\n".join(pdf_text(data))
        return data.decode("utf-8-sig", errors="replace")
    except Exception as e:  # noqa: BLE001
        return f"[Could not read {name}: {e}]"


def for_report(db: Session, event: Event, themes: list[str], max_items: int = 4) -> tuple[list[str], int]:
    """Pick principles relevant to this company's feedback themes, adapted to the event format."""
    v = current(db)
    c = v.content
    principles = c.get("principles", [])
    by_id = {p["id"]: p for p in principles}
    relevant = [p for p in principles if set(p.get("themes", [])) & set(themes) and p["id"] != "ownership"][:2]
    general = [by_id[i] for i in ("next_step", "prompt") if i in by_id and by_id[i] not in relevant]
    lines = [p["text"] for p in relevant + general][: max(1, max_items - 1)]
    fmt = c.get("format_guidance", {}).get(event.format)
    if fmt:
        lines.insert(min(len(relevant), len(lines)), fmt)
    # The consistent principle from the source documents always closes the section.
    if "ownership" in by_id:
        lines.append(by_id["ownership"]["text"])
    return lines, v.version
