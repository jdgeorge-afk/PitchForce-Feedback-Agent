"""EcoVG calendar retrieval and session recommendations.

- Sources are configurable (CALENDAR_URLS); only allow-listed official hosts are followed.
- Dates are verified from each event's detail page (its ICS file, or the Date/Time block) because the
  listing omits the year. Sessions without a verified date are never presented as upcoming.
- "Upcoming" is judged against the report-generation time, not the original event date.
- Results are cached (CALENDAR_CACHE_HOURS) with an operator refresh action. A failed refresh keeps
  the previous cache and never blocks analysis or report work.
- Nothing is invented: titles, times, and URLs come only from the official pages.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import utcnow
from ..models import AppSetting, CalendarRecommendation, CalendarSession, Company
from .net import FetchBlocked, fetch

TZ_ALIASES = {"US/Pacific": "America/Los_Angeles", "US/Mountain": "America/Denver", "US/Central": "America/Chicago",
              "US/Eastern": "America/New_York", "PT": "America/Los_Angeles", "PDT": "America/Los_Angeles",
              "PST": "America/Los_Angeles", "MT": "America/Denver", "ET": "America/New_York", "CT": "America/Chicago"}

# Words that connect a session to feedback themes. Weighted by specificity.
THEME_KEYWORDS = {
    "timeline_spend": ["use of funds", "capital plan", "budget", "milestone", "runway", "financial model", "capitalization"],
    "answer_length": ["pitch", "presentation", "q&a", "investor questions", "storytelling", "communicat"],
    "ip_competition": ["intellectual property", "patent", " ip ", "competitive", "moat"],
    "investor_return": ["exit", "liquidity", "return", "valuation", "investor perspective", "what investors"],
    "slides": ["pitch deck", "deck", "presentation", "storytelling"],
    "regulatory": ["fda", "regulatory", "reimbursement", "clinical"],
    "market": ["go-to-market", "sales", "customer", "market"],
    "traction": ["traction", "customer", "revenue"],
    "team": ["team", "hiring", "board", "leadership"],
    "financials": ["fundrais", "valuation", "cap table", "term sheet", "safe", "raise", "due diligence"],
    "clarity": ["pitch", "storytelling", "presentation", "messag"],
}
NOT_LEARNING = re.compile(r"\bpitchforce\b|\bpitch competition\b|\bdemo day\b", re.I)


@dataclass
class ParsedSession:
    title: str
    detail_url: str
    start_utc: datetime | None
    tz: str
    start_raw: str
    description: str
    verified: bool


def _unfold_ics(text: str) -> list[str]:
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def parse_ics(text: str) -> dict | None:
    lines = _unfold_ics(text)
    ev, inside = {}, False
    for ln in lines:
        if ln == "BEGIN:VEVENT":
            inside = True
            continue
        if ln == "END:VEVENT":
            break
        if not inside or ":" not in ln:
            continue
        key, val = ln.split(":", 1)
        name, *params = key.split(";")
        ev[name] = (val.replace("\\,", ",").replace("\\;", ";").replace("\\n", "\n"), dict(p.split("=", 1) for p in params if "=" in p))
    if "DTSTART" not in ev or "SUMMARY" not in ev:
        return None
    val, params = ev["DTSTART"]
    tzid = TZ_ALIASES.get(params.get("TZID", ""), params.get("TZID", ""))
    try:
        if val.endswith("Z"):
            start = datetime.strptime(val, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        else:
            naive = datetime.strptime(val[:15], "%Y%m%dT%H%M%S")
            start = naive.replace(tzinfo=ZoneInfo(tzid)) if tzid else None
    except Exception:
        start = None
    import html as _html

    desc = _html.unescape(ev.get("DESCRIPTION", ("", {}))[0])
    return {"title": _html.unescape(ev["SUMMARY"][0]).strip(), "start": start, "tz": tzid, "raw": val, "description": desc}


DATE_BLOCK = re.compile(
    r"Date:\s*\|?\s*(\d{1,2} \w{3} \d{4})\s*\|?\s*Time:\s*\|?\s*(\d{1,2}:\d{2}\s*[ap]m)(?:[^|]*?\b(P[DS]T|M[DS]T|E[DS]T|C[DS]T)\b)?",
    re.I,
)


def parse_detail_html(html: str) -> tuple[datetime | None, str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for s in soup(["script", "style"]):
        s.decompose()
    txt = soup.get_text(" | ", strip=True)
    m = DATE_BLOCK.search(txt)
    if not m:
        return None, "", ""
    tz = TZ_ALIASES.get((m.group(3) or "").upper(), "")
    try:
        naive = datetime.strptime(f"{m.group(1)} {m.group(2).replace(' ', '')}", "%d %b %Y %I:%M%p")
    except ValueError:
        return None, "", ""
    return (naive.replace(tzinfo=ZoneInfo(tz)) if tz else None), tz, m.group(0)


def scrape_listing(url: str) -> list[tuple[str, str]]:
    final, _, html = fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, str] = {}
    for a in soup.select('a[href*="/events/"]'):
        href = a.get("href", "")
        title = a.get_text(" ", strip=True)
        if not href or title.lower() in ("read more", "view all events", "") or "/events/" not in href:
            continue
        from urllib.parse import urljoin, urlparse

        full = urljoin(final, href)
        p = urlparse(full)
        full = f"{p.scheme}://{p.hostname}{p.path}"  # normalize (drop :443, query)
        out.setdefault(full, title)
    return list(out.items())


def retrieve(urls: list[str] | None = None) -> tuple[list[ParsedSession], list[str]]:
    urls = urls or get_settings().calendar_url_list
    sessions, errors = [], []
    for url in urls:
        try:
            listing = scrape_listing(url)
        except (FetchBlocked, Exception) as e:  # noqa: BLE001
            errors.append(f"{url}: {e}")
            continue
        for detail, title in listing:
            start, tz, raw, desc, verified = None, "", "", "", False
            try:
                _, _, ics = fetch(detail.rstrip("/") + "/invite_ics.ics")
                parsed = parse_ics(ics)
                if parsed and parsed["start"]:
                    start, tz, raw, desc, verified = parsed["start"], parsed["tz"], parsed["raw"], parsed["description"], True
                    title = parsed["title"] or title
            except Exception:  # noqa: BLE001
                pass
            if not verified:
                try:
                    _, _, html = fetch(detail)
                    start, tz, raw = parse_detail_html(html)
                    verified = start is not None
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{detail}: {e}")
            sessions.append(ParsedSession(title, detail, start.astimezone(timezone.utc) if start else None, tz, raw, desc[:4000], verified))
    return sessions, errors


def refresh(db: Session, force: bool = False, fetcher=retrieve) -> dict:
    s = get_settings()
    meta = db.get(AppSetting, "calendar_cache") or AppSetting(key="calendar_cache", value={})
    last = meta.value.get("fetched_at")
    if not force and last and datetime.fromisoformat(last) > utcnow() - timedelta(hours=s.calendar_cache_hours):
        return {**meta.value, "cached": True}
    sessions, errors = fetcher()
    now = utcnow()
    if sessions:
        for ps in sessions:
            row = db.scalar(select(CalendarSession).where(CalendarSession.detail_url == ps.detail_url))
            row = row or CalendarSession(detail_url=ps.detail_url, source_url=",".join(s.calendar_url_list), title=ps.title)
            row.title, row.start_raw, row.timezone, row.description = ps.title, ps.start_raw, ps.tz, ps.description
            row.start_at_utc = ps.start_utc.replace(tzinfo=None) if ps.start_utc else None
            row.verified_on_detail = ps.verified
            row.retrieved_at = now
            db.add(row)
    status = "ok" if sessions and not errors else ("partial" if sessions else "failed")
    meta.value = {"fetched_at": now.isoformat(timespec="seconds"), "status": status, "errors": errors[:10],
                  "count": len(sessions), "sources": s.calendar_url_list}
    db.merge(meta)
    db.flush()
    return meta.value


def upcoming(db: Session, now: datetime | None = None) -> list[CalendarSession]:
    now = now or utcnow()
    return list(db.scalars(select(CalendarSession).where(CalendarSession.verified_on_detail.is_(True),
                                                         CalendarSession.start_at_utc > now).order_by(CalendarSession.start_at_utc)))


def relevance(session: CalendarSession, themes: list[str]) -> tuple[int, list[str]]:
    if NOT_LEARNING.search(session.title):
        return 0, []
    text = f" {session.title} {session.description} ".lower()
    score, why = 0, []
    for th in themes:
        for kw in THEME_KEYWORDS.get(th, []):
            if kw in text:
                score += 2 if kw in session.title.lower() else 1
                why.append(th)
                break
    return score, sorted(set(why))


def recommend(db: Session, company: Company, themes: list[str], now: datetime | None = None, limit: int = 3,
              min_score: int = 2) -> list[CalendarRecommendation]:
    """Up to ``limit`` genuinely relevant verified upcoming sessions. Keeps operator approvals/rejections."""
    existing = {r.session_id: r for r in db.scalars(select(CalendarRecommendation).where(CalendarRecommendation.company_id == company.id))}
    for r in existing.values():
        if r.status == "proposed":
            db.delete(r)
    picked = []
    for sess in upcoming(db, now):
        if sess.id in existing and existing[sess.id].status == "rejected":
            continue
        sc, why = relevance(sess, themes)
        if sc >= min_score:
            picked.append((sc, sess, why))
    picked.sort(key=lambda x: (-x[0], x[1].start_at_utc))
    out = []
    from ..analysis.heuristics import THEME_LABELS

    for sc, sess, why in picked[:limit]:
        if sess.id in existing and existing[sess.id].status == "approved":
            out.append(existing[sess.id])
            continue
        from ..reports.compose import _lc

        reason = "Relates to panel feedback on " + ", ".join(_lc(THEME_LABELS.get(w, w)) for w in why) + "."
        rec = CalendarRecommendation(company_id=company.id, session_id=sess.id, reason=reason, is_upcoming=True)
        db.add(rec)
        out.append(rec)
    db.flush()
    return out


def cache_status(db: Session) -> dict:
    meta = db.get(AppSetting, "calendar_cache")
    return meta.value if meta else {"status": "never", "fetched_at": None}
