from datetime import date

from pitchforce.models import Company, Event, JudgeAttendance, Person, Score
from pitchforce.services.scoring import ScoreInputs, calculate, compute_for_company, fmt_score, mark_duplicates, rank_companies


def inp(scores, missing=(), expected=6, policy="one_absent_mean", announced=None):
    return ScoreInputs(expected, 5.0, policy, [(f"J{i}", v) for i, v in enumerate(scores)], list(missing), announced=announced)


def test_six_valid_scores_sum():
    r = calculate(inp([3, 4, 5, 2, 3, 4]))
    assert r.status == "verified" and r.adjusted_total == 21 and r.denominator == 30 and r.imputed_contribution is None


def test_five_threes_plus_one_confirmed_absent_is_18_of_30():
    r = calculate(inp([3, 3, 3, 3, 3], missing=["Absent Judge"]))
    assert r.status == "verified_with_imputation"
    assert r.raw_sum == 15 and r.mean == 3 and r.imputed_contribution == 3
    assert r.adjusted_total == 18 and r.denominator == 30


def test_missing_source_scores_do_not_trigger_imputation():
    r = calculate(inp([3, 3, 3, 3, 3]))  # sixth judge simply missing from the file
    assert r.status == "incomplete" and r.adjusted_total is None and r.imputed_contribution is None


def test_two_confirmed_missing_needs_explicit_policy():
    assert calculate(inp([4, 4, 4, 4], missing=["A", "B"])).status == "incomplete"
    r = calculate(inp([4, 4, 4, 4], missing=["A", "B"], policy="mean_for_confirmed_missing"))
    assert r.status == "verified_with_imputation" and r.adjusted_total == 24


def test_all_scores_missing_is_unavailable_and_never_reverse_engineered():
    r = calculate(inp([]))
    assert r.status == "unavailable" and r.adjusted_total is None and r.raw_sum is None
    r2 = calculate(inp([], announced=21))
    assert r2.status == "announced_only" and r2.adjusted_total is None and r2.announced == 21 and r2.valid_count == 0


def test_announced_total_differs_from_calculated():
    r = calculate(inp([4, 4, 4, 4, 4, 4], announced=23))
    assert r.adjusted_total == 24 and r.discrepancy == -1


def test_fractional_scores_and_full_precision():
    r = calculate(inp([3.5, 4.25, 4, 4, 4, 4]))
    assert r.adjusted_total == 23.75
    assert fmt_score(23.75, 1) == "23.8" and fmt_score(23.75, 2) == "23.75"


def test_ties_preserved_without_tiebreaker():
    ranks = rank_companies([("a", 21.0), ("b", 24.0), ("c", 21.0), ("d", None), ("e", 18.0)])
    by = {cid: (rank, tied) for cid, v, rank, tied in ranks}
    assert by["b"] == (1, False) and by["a"] == (2, True) and by["c"] == (2, True) and by["e"] == (4, False)
    assert by["d"] == (None, False)


def test_invalid_out_of_range_and_duplicate_scores_flagged(db):
    from pitchforce.parsing.rosters import parse_score_value

    assert parse_score_value("7", 1, 5, True)[1] == "out_of_range"
    assert parse_score_value("abc", 1, 5, True)[1] == "invalid"
    assert parse_score_value("3.5", 1, 5, False)[1] == "invalid"  # event rule: integers only
    assert parse_score_value("", 1, 5, True)[1] == "missing"
    ev = Event(name="E", event_date=date(2026, 1, 1))
    db.add(ev)
    db.flush()
    c = Company(event_id=ev.id, display_name="Acme")
    j = Person(event_id=ev.id, display_name="Judge One", role="judge")
    db.add_all([c, j])
    db.flush()
    for v in ("4", "5"):
        db.add(Score(event_id=ev.id, company_id=c.id, judge_person_id=j.id, judge_label_raw="Judge One", value_raw=v, value=float(v)))
        db.flush()
    mark_duplicates(db, ev)
    statuses = sorted(s.status for s in db.query(Score).all())
    assert statuses == ["duplicate", "valid"]
    res = compute_for_company(db, ev, c)
    assert res.valid_count == 1


def test_db_absence_rule_applies_only_to_confirmed_absence(db):
    ev = Event(name="E", event_date=date(2026, 1, 1), expected_judge_count=6)
    db.add(ev)
    db.flush()
    c = Company(event_id=ev.id, display_name="Acme")
    db.add(c)
    judges = [Person(event_id=ev.id, display_name=f"J{i}", role="judge") for i in range(6)]
    db.add_all(judges)
    db.flush()
    for j in judges[:5]:
        db.add(Score(event_id=ev.id, company_id=c.id, judge_person_id=j.id, value_raw="3", value=3.0))
    db.flush()
    r = compute_for_company(db, ev, c)
    assert r.status == "incomplete"  # sixth judge unknown: no imputation
    db.add(JudgeAttendance(event_id=ev.id, person_id=judges[5].id, status="absent_confirmed"))
    db.flush()
    r = compute_for_company(db, ev, c)
    assert r.status == "verified_with_imputation" and r.adjusted_total == 18 and r.denominator == 30
