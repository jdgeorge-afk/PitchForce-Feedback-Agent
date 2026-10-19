"""Identity matching, related entities, and the full sample pipeline (synthetic data)."""
from pitchforce.models import (
    AnnouncedTotal,
    CompanyAlias,
    CompanySegment,
    EntityRelationship,
    Feedback,
    JudgeAttendance,
    Person,
    PersonAlias,
    Question,
    ReportSection,
    ReviewItem,
)
from pitchforce.services.scoring import current_result

from .conftest import company


def test_sample_processing_results(sample):
    env, eid = sample
    db = env.session()
    hema = company(db, eid, "Hemaflow Bio")
    r = current_result(db, hema.id)
    assert r.status == "announced_only" and r.announced_value == 21 and r.adjusted_total is None and r.valid_count == 0
    bw = company(db, eid, "Brightwell Diagnostics")
    r = current_result(db, bw.id)
    assert r.status == "verified_with_imputation" and r.adjusted_total == 18 and r.denominator == 30
    corv = company(db, eid, "Corvane Pharma")
    assert current_result(db, corv.id).status == "incomplete"  # blank cell = missing data, no imputation
    owl = company(db, eid, "Owlvision")
    from pitchforce.models import Score

    assert db.query(Score).filter_by(company_id=owl.id, status="out_of_range").count() == 1
    db.close()


def test_hemaflow_segments_questions_feedback(sample):
    env, eid = sample
    db = env.session()
    hema = company(db, eid, "Hemaflow Bio")
    segs = {s.kind: s for s in db.query(CompanySegment).filter_by(company_id=hema.id)}
    assert {"pitch", "qa", "feedback", "score", "award"} <= set(segs)
    assert segs["pitch"].start_s < segs["qa"].start_s < segs["feedback"].start_s < segs["score"].start_s
    qs = db.query(Question).filter_by(company_id=hema.id, parent_id=None).all()
    assert len([q for q in qs if q.asker_role == "investor"]) == 3
    multi = next(q for q in qs if "animal model" in q.original_excerpt)
    assert db.query(Question).filter_by(parent_id=multi.id).count() >= 2  # multi-part kept traceable
    fb = db.query(Feedback).filter_by(company_id=hema.id).all()
    speakers = {f.speaker_label for f in fb if f.kind != "silent"}
    assert speakers == {"Priya Nair", "Jordan Avery", "Sam Kowalski", "Tom Becker", "Lena Ortiz"}
    themes = {f.speaker_label: f.theme for f in fb if f.kind == "recommendation"}
    assert themes["Priya Nair"] == "timeline_spend" and themes["Jordan Avery"] == "answer_length"
    assert themes["Sam Kowalski"] == "ip_competition" and themes["Tom Becker"] == "investor_return"
    at = db.query(AnnouncedTotal).filter_by(company_id=hema.id).one()
    assert at.value == 21 and at.source_kind == "transcript" and "twenty one" in at.excerpt
    db.close()


def test_alias_case_requires_review_and_preserves_spelling(fresh_sample):
    env, eid = fresh_sample
    db = env.session()
    owl = company(db, eid, "Owlvision")
    item = db.query(ReviewItem).filter(ReviewItem.event_id == eid, ReviewItem.dedupe_key.like("block-slot:Leo Park:%")).one()
    assert item.status == "open" and "Leo Park" in item.title and "Sam Whitfield" in item.title
    proposed = {a.alias: a.status for a in db.query(CompanyAlias).filter_by(company_id=owl.id)}
    assert proposed.get("SightCaption") == "proposed"
    sam = db.query(Person).filter_by(company_id=owl.id, role="presenter").one()
    # Operator confirms with their own spelling; the roster spelling is preserved as-is.
    from pitchforce.services import matching

    matching.add_person_alias(db, sam, "Sam Whitfeld", "operator", status="confirmed", note="Spelling from operator message")
    for a in db.query(PersonAlias).filter_by(person_id=sam.id, alias="Leo Park"):
        a.status = "confirmed"
    for a in db.query(CompanyAlias).filter_by(company_id=owl.id, alias="SightCaption"):
        a.status = "confirmed"
    db.commit()
    assert sam.display_name == "Sam Whitfield" and sam.name_raw == "Sam Whitfield"
    # Regenerate: the confirmed association now matches with certainty and persists.
    from pitchforce.jobs.worker import drain
    from pitchforce.services.processing import queue_processing

    queue_processing(db, db.get(type(owl.event), eid), None)
    db.commit()
    drain()
    db.expire_all()
    aliases = {a.alias: a.status for a in db.query(PersonAlias).filter_by(person_id=sam.id)}
    assert aliases["Sam Whitfeld"] == "confirmed" and aliases["Leo Park"] == "confirmed"
    pitch = db.query(CompanySegment).filter_by(company_id=owl.id, kind="pitch").one()
    assert pitch.status != "needs_review"
    db.close()


def test_related_entities_not_merged(sample):
    env, eid = sample
    db = env.session()
    corv = company(db, eid, "Corvane Pharma")
    rels = {r.subject_label: r for r in db.query(EntityRelationship).filter_by(company_id=corv.id)}
    assert "Petrix" in rels and rels["Petrix"].status == "proposed"
    assert "Corvain Pharma" in rels and rels["Corvain Pharma"].relation == "parent" and rels["Corvain Pharma"].status == "proposed"
    assert not db.query(CompanyAlias).filter_by(company_id=corv.id, status="confirmed").count()  # nothing collapsed
    rev = db.query(ReviewItem).filter(ReviewItem.dedupe_key == f"rel:{corv.id}:corvainpharma").one()
    assert "transcription" in rev.detail
    db.close()


def test_absence_mentioned_by_host_is_only_a_proposal(sample):
    env, eid = sample
    db = env.session()
    item = db.query(ReviewItem).filter(ReviewItem.event_id == eid, ReviewItem.dedupe_key == "absence:morgan").one()
    assert item.status == "open" and "Morgan" in item.title
    morgan = db.query(Person).filter_by(event_id=eid, display_name="Morgan Lee").one()
    att = db.query(JudgeAttendance).filter_by(person_id=morgan.id).one()
    assert att.source == "roster"  # the roster said absent; the transcript mention alone changes nothing
    db.close()


def test_report_sections_and_constraints(sample):
    env, eid = sample
    db = env.session()
    from pitchforce.reports.compose import report_word_count, sections

    hema = company(db, eid, "Hemaflow Bio")
    secs = {s.key: s.text for s in sections(db, hema)}
    assert set(secs) == {"header", "result", "assessment", "worked", "questions", "priorities", "best_practices", "replay",
                         "learning", "advisory"}
    assert "has not been independently recalculated" in secs["result"]
    assert "No respondent selected investment interest" in secs["assessment"]
    assert "not available" in secs["replay"]  # no video in this sample run
    assert "next seven days" not in " ".join(secs.values()).lower()
    assert secs["priorities"].count("### ") == 3
    for kw in ("Why it matters to investors", "What to change", "A stronger version", "Evidence:"):
        assert secs["priorities"].count(kw) == 3
    assert "https://ecovg.com/entrepreneur-services/" in secs["advisory"] and "$" not in secs["advisory"]
    assert "events.ecovg.com/events" in secs["learning"]  # no sessions verified offline -> link to calendar, nothing invented
    assert 550 <= report_word_count(db, hema) <= 950
    db.close()


def test_manual_edits_survive_unrelated_regeneration(fresh_sample):
    env, eid = fresh_sample
    db = env.session()
    from pitchforce.reports.compose import compose

    hema = company(db, eid, "Hemaflow Bio")
    ev = hema.event
    sec = db.query(ReportSection).filter_by(company_id=hema.id, key="worked").one()
    sec.edited_text = "Operator wrote this."
    other = db.query(ReportSection).filter_by(company_id=hema.id, key="assessment").one()
    other.input_hash = "changed"  # simulate changed evidence for another section
    db.commit()
    actions = compose(db, ev, hema)
    assert actions["assessment"] == "updated" and actions["worked"] == "unchanged"
    assert db.query(ReportSection).filter_by(company_id=hema.id, key="worked").one().text == "Operator wrote this."
    # When the edited section's own evidence changes, the edit is kept and flagged stale.
    sec.input_hash = "old"
    db.commit()
    assert compose(db, ev, hema)["worked"] == "stale_kept"
    s2 = db.query(ReportSection).filter_by(company_id=hema.id, key="worked").one()
    assert s2.text == "Operator wrote this." and s2.stale
    db.close()
