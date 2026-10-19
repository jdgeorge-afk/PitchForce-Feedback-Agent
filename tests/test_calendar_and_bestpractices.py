import io
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from docx import Document

from pitchforce.models import CalendarRecommendation, CalendarSession, Company, Event
from pitchforce.services import bestpractices, calendar
from pitchforce.services.calendar import ParsedSession, parse_detail_html, parse_ics
from pitchforce.services.net import FetchBlocked, check_url

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Pitch Deck Workshop: Telling Your Story to Investors
DTSTART;TZID=US/Pacific;VALUE=DATE-TIME:20261105T110000
DESCRIPTION: Learn how to structure your pitch deck\\, answer investor
  questions\\, and present your use of funds.
END:VEVENT
END:VCALENDAR
"""


def test_parse_ics_and_detail_html():
    p = parse_ics(ICS)
    assert p["title"].startswith("Pitch Deck Workshop") and p["tz"] == "America/Los_Angeles"
    assert p["start"].astimezone(timezone.utc).hour == 19  # 11:00 PST = 19:00 UTC
    html = "<div>Date: | 26 Oct 2026 | Time: | 11:00am - 12:00pm PDT</div>"
    start, tz, raw = parse_detail_html(html)
    assert start.year == 2026 and tz == "America/Los_Angeles"


def _fake(now):
    def fetcher():
        return ([
            ParsedSession("Pitch Deck Workshop: Telling Your Story to Investors", "https://events.ecovg.com/events/deck",
                          now + timedelta(days=10), "America/Los_Angeles", "x", "pitch deck use of funds investor questions", True),
            ParsedSession("Fundraising Basics (past)", "https://events.ecovg.com/events/past", now - timedelta(days=3),
                          "America/Los_Angeles", "x", "fundraising valuation pitch", True),
            ParsedSession("Unverified Session", "https://events.ecovg.com/events/unverified", None, "", "", "pitch deck", False),
            ParsedSession("PitchForce Palo Alto", "https://events.pitch-force.com/events/pf", now + timedelta(days=5), "", "x",
                          "pitch presentation investors", True),
            ParsedSession("Mental Health Monday", "https://events.ecovg.com/events/mh", now + timedelta(days=2), "", "x",
                          "founder wellbeing conversation", True),
        ], [])
    return fetcher


def test_recommendations_exclude_past_unverified_and_unrelated(db):
    now = datetime.now(timezone.utc)
    res = calendar.refresh(db, force=True, fetcher=_fake(now))
    assert res["status"] == "ok" and res["count"] == 5
    ev = Event(name="E", event_date=(now - timedelta(days=30)).date())  # event date in the past: irrelevant to "upcoming"
    db.add(ev)
    db.flush()
    c = Company(event_id=ev.id, display_name="Acme")
    db.add(c)
    db.flush()
    recs = calendar.recommend(db, c, ["slides", "timeline_spend"])
    titles = [db.get(CalendarSession, r.session_id).title for r in recs]
    assert titles == ["Pitch Deck Workshop: Telling Your Story to Investors"]
    assert "past" not in " ".join(titles).lower() and "Unverified" not in " ".join(titles)


def test_no_relevant_session_links_calendar_without_inventing(db):
    from pitchforce.reports.compose import Evidence, s_learning

    ev = Event(name="E", event_date=datetime.now().date())
    db.add(ev)
    db.flush()
    c = Company(event_id=ev.id, display_name="Acme")
    db.add(c)
    db.flush()
    text, _ = s_learning(Evidence(db, ev, c))
    assert "did not find an upcoming EcoVG session" in text and "https://events.ecovg.com/events" in text
    assert "replay" not in text.lower()


def test_failed_refresh_keeps_cache(db):
    now = datetime.now(timezone.utc)
    calendar.refresh(db, force=True, fetcher=_fake(now))
    res = calendar.refresh(db, force=True, fetcher=lambda: ([], ["network down"]))
    assert res["status"] == "failed" and db.query(CalendarSession).count() == 5


@pytest.mark.parametrize("url", ["http://events.ecovg.com/x", "https://evil.example.com/x", "https://user:pw@events.ecovg.com/x",
                                 "https://localhost/x", "file:///etc/passwd"])
def test_ssrf_guard(url):
    with pytest.raises(FetchBlocked):
        check_url(url, allowed={"ecovg.com", "events.ecovg.com", "localhost"})


def _docx(paras):
    d = Document()
    for p in paras:
        d.add_paragraph(p)
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def test_best_practice_zip_import_flags_duplicates_archives_and_contradictions():
    current = _docx(["Founders own investor follow-up.", "We request the company to: follow up on your behalf and guarantee investment outcomes."])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("6. Company Cheat Sheet/PRESENTING COMPANY CHEAT SHEET.docx", current)
        z.writestr("6. Company Cheat Sheet/COPY.docx", current)
        z.writestr("6. Company Cheat Sheet/Old/PRESENTING COMPANY CHEAT SHEET v1.docx", _docx(["Booth hours 10 a.m.-noon"]))
        z.writestr("../escape.docx", current)
    res = bestpractices.import_documents("cheat.zip", buf.getvalue())
    assert any("unsafe path" in s for s in res["skipped"])
    assert len(res["duplicates"]) == 1
    assert any(d["archived"] for d in res["documents"]) and all("Old/" not in p for p in res["preferred"])
    issues = {i["issue"] for i in res["issues"]}
    assert "Mentions follow-up “on your behalf”" in issues and "Mentions guaranteed outcomes" in issues


def test_default_best_practices_flag_known_inconsistency(db):
    v = bestpractices.current(db)
    assert any(i["id"] == "responsibility-contradiction" for i in v.issues)
    ev = Event(name="E", event_date=datetime.now().date(), format="virtual")
    db.add(ev)
    db.flush()
    lines, _ = bestpractices.for_report(db, ev, ["answer_length"])
    joined = " ".join(lines)
    assert "video call" in joined and "booth" not in joined.lower()
    assert "10 a.m." not in joined and "guarantee" in joined  # "does not guarantee" principle
    ev.format = "in_person"
    lines2, _ = bestpractices.for_report(db, ev, [])
    assert any("booth" in ln.lower() for ln in lines2)


_ = CalendarRecommendation
