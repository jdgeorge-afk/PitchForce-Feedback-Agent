"""Judge score calculation.

Rules (see docs/SCORING.md):
- Individual scores and announced totals are stored separately and never derived from each other.
- Only scores with status ``valid`` from a resolved judge count.
- The missing-judge adjustment applies only to *confirmed* missing judge slots (confirmed absence or
  confirmed abstention). Missing source data, unresolved identities, and invalid/duplicate scores
  never trigger imputation.
- Policy ``one_absent_mean`` (default) applies the adjustment only when exactly one slot is confirmed
  missing; ``mean_for_confirmed_missing`` (explicit event policy) allows more; ``none`` never imputes.
- Full precision is kept; rounding happens only for display.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import AnnouncedTotal, Company, Event, JudgeAttendance, Person, Score, ScoreResult
from ..util import stable_hash


@dataclass
class ScoreInputs:
    expected_judges: int
    score_max: float
    policy: str
    valid_scores: list[tuple[str, float]]  # (judge label, value)
    confirmed_missing: list[str]  # judge labels confirmed absent / abstained without a valid score
    excluded: list[dict] = field(default_factory=list)  # invalid, duplicate, unresolved, missing-data
    announced: float | None = None
    announced_denominator: float | None = None


@dataclass
class ScoreCalc:
    status: str
    valid_count: int
    raw_sum: float | None
    mean: float | None
    confirmed_missing: int
    imputed_contribution: float | None
    adjusted_total: float | None
    denominator: float | None
    announced: float | None
    discrepancy: float | None
    explanation: list[str]


def calculate(inp: ScoreInputs) -> ScoreCalc:
    n = len(inp.valid_scores)
    expl: list[str] = []
    denominator = inp.expected_judges * inp.score_max
    if n == 0:
        status = "announced_only" if inp.announced is not None else "unavailable"
        expl.append("No verified individual judge scores, so no total was calculated.")
        if inp.announced is not None:
            expl.append("The host-announced total is shown as announced; it has not been independently recalculated.")
        return ScoreCalc(status, 0, None, None, len(inp.confirmed_missing), None, None,
                         inp.announced_denominator or None, inp.announced, None, expl)

    raw_sum = sum(v for _, v in inp.valid_scores)
    mean = raw_sum / n
    missing_confirmed = len(inp.confirmed_missing)
    accounted = n + missing_confirmed
    imputed = None
    adjusted = None
    status = "incomplete"

    if accounted > inp.expected_judges:
        expl.append(f"More judges accounted for ({accounted}) than expected ({inp.expected_judges}); check the roster.")
    elif n == inp.expected_judges:
        adjusted = raw_sum
        status = "verified"
        expl.append(f"All {n} expected judges submitted valid scores; total is their sum.")
    elif accounted == inp.expected_judges and missing_confirmed > 0:
        allowed = (
            (inp.policy == "one_absent_mean" and missing_confirmed == 1)
            or inp.policy == "mean_for_confirmed_missing"
        )
        if allowed:
            imputed = mean * missing_confirmed
            adjusted = raw_sum + imputed
            status = "verified_with_imputation"
            expl.append(
                f"{missing_confirmed} judge slot(s) confirmed missing; per the event's approved rule the mean of the "
                f"{n} submitted scores ({mean:g}) was added for each missing slot (imputed, not a judge's score)."
            )
        else:
            expl.append(
                f"{missing_confirmed} judge slots are confirmed missing, but the event policy ({inp.policy}) does not "
                "allow imputing that many. Total left incomplete."
            )
    else:
        unaccounted = inp.expected_judges - accounted
        expl.append(
            f"{unaccounted} expected judge score(s) are not accounted for (missing source data or unresolved). "
            "No imputation is applied for missing data."
        )
    discrepancy = None
    if inp.announced is not None and adjusted is not None:
        discrepancy = inp.announced - adjusted
        if abs(discrepancy) > 1e-9:
            expl.append(f"Announced total ({inp.announced:g}) differs from the calculated total ({adjusted:g}) by {discrepancy:+g}.")
    return ScoreCalc(status, n, raw_sum, mean, missing_confirmed, imputed, adjusted,
                     denominator, inp.announced, discrepancy, expl)


def fmt_score(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "—"
    r = round(value, decimals)
    return f"{r:.{decimals}f}".rstrip("0").rstrip(".") if decimals else f"{r:.0f}"


# ---------------------------------------------------------------------------
# Database integration
# ---------------------------------------------------------------------------
def mark_duplicates(db: Session, event: Event) -> None:
    """Flag duplicate scores (same company + judge). Earlier rows stay valid; later ones are duplicates
    unless they carry the same value from a different source (then also duplicate but noted)."""
    rows = list(db.scalars(select(Score).where(Score.event_id == event.id, Score.status.in_(["valid", "duplicate"]))
                           .order_by(Score.created_at, Score.id)))
    seen: dict[tuple, Score] = {}
    for s in rows:
        if not s.company_id or not s.judge_person_id:
            continue
        key = (s.company_id, s.judge_person_id)
        if key in seen:
            first = seen[key]
            s.status = "duplicate"
            same = first.value == s.value
            s.status_reason = (
                f"Duplicate of an earlier score for the same judge and company ({'same' if same else 'different'} value: "
                f"{first.value_raw} vs {s.value_raw})."
            )
        else:
            seen[key] = s
            if s.status == "duplicate":
                s.status = "valid"
                s.status_reason = ""


def gather_inputs(db: Session, event: Event, company: Company) -> ScoreInputs:
    scores = list(db.scalars(select(Score).where(Score.company_id == company.id)))
    valid, excluded = [], []
    judges_with_valid = set()
    for s in scores:
        if s.status == "valid" and s.value is not None and s.judge_person_id:
            p = db.get(Person, s.judge_person_id)
            valid.append((p.display_name if p else s.judge_label_raw, s.value))
            judges_with_valid.add(s.judge_person_id)
        else:
            excluded.append({"judge": s.judge_label_raw, "value": s.value_raw, "status": s.status, "reason": s.status_reason})
    missing = []
    for att in db.scalars(select(JudgeAttendance).where(JudgeAttendance.event_id == event.id)):
        if att.status in ("absent_confirmed", "abstained_confirmed") and att.person_id not in judges_with_valid:
            p = db.get(Person, att.person_id)
            missing.append(p.display_name if p else att.person_id)
    ann = db.scalar(
        select(AnnouncedTotal).where(AnnouncedTotal.company_id == company.id, AnnouncedTotal.status != "rejected")
        .order_by(AnnouncedTotal.status.desc(), AnnouncedTotal.created_at)
    )
    return ScoreInputs(
        expected_judges=event.expected_judge_count,
        score_max=event.score_max,
        policy=event.missing_judge_policy,
        valid_scores=sorted(valid),
        confirmed_missing=sorted(missing),
        excluded=excluded,
        announced=ann.value if ann else None,
        announced_denominator=ann.denominator if ann else None,
    )


def compute_for_company(db: Session, event: Event, company: Company) -> ScoreResult:
    inp = gather_inputs(db, event, company)
    calc = calculate(inp)
    h = stable_hash(inp.__dict__)
    current = db.scalar(select(ScoreResult).where(ScoreResult.company_id == company.id, ScoreResult.is_current.is_(True)))
    if current and current.input_hash == h:
        return current
    db.execute(update(ScoreResult).where(ScoreResult.company_id == company.id).values(is_current=False))
    res = ScoreResult(
        company_id=company.id, policy=inp.policy, expected_judges=inp.expected_judges, valid_count=calc.valid_count,
        raw_sum=calc.raw_sum, mean=calc.mean, confirmed_missing=calc.confirmed_missing,
        imputed_contribution=calc.imputed_contribution, adjusted_total=calc.adjusted_total,
        denominator=calc.denominator, status=calc.status, announced_value=calc.announced,
        discrepancy=calc.discrepancy, input_hash=h, is_current=True,
        details={"valid_scores": inp.valid_scores, "confirmed_missing": inp.confirmed_missing,
                 "excluded": inp.excluded, "explanation": calc.explanation,
                 "announced_denominator": inp.announced_denominator},
    )
    db.add(res)
    db.flush()
    return res


def current_result(db: Session, company_id: str) -> ScoreResult | None:
    return db.scalar(select(ScoreResult).where(ScoreResult.company_id == company_id, ScoreResult.is_current.is_(True)))


def rank_companies(results: list[tuple[str, float | None]]) -> list[tuple[str, float | None, int | None, bool]]:
    """Competition ranking with ties preserved (1, 2, 2, 4). No tiebreaker is invented."""
    scored = sorted([r for r in results if r[1] is not None], key=lambda r: -r[1])
    out = []
    prev, rank = None, 0
    for i, (cid, v) in enumerate(scored, start=1):
        if v != prev:
            rank = i
            prev = v
        tied = sum(1 for _, x in scored if x == v) > 1
        out.append((cid, v, rank, tied))
    out += [(cid, None, None, False) for cid, v in results if v is None]
    return out
