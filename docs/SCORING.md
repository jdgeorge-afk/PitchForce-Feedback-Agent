# Judge scoring rules

Implemented in `pitchforce/services/scoring.py`; tested in `tests/test_scoring.py`.

- Scores use the event's scale (default 1–5). Fractional scores are allowed unless the event turns them off.
- Individual scores (`Score`) and host-announced totals (`AnnouncedTotal`) are stored separately. Neither is derived from the other.
- A score counts only if it is **valid**, linked to a roster judge (exact name or confirmed alias), and linked to a presenting company.

**Status of each score row:**

| Status | Meaning |
|---|---|
| `valid` | Counts toward the total |
| `missing` | Blank in the source. This is missing data, not an absence. |
| `invalid` | Not a number, or a fraction when the event requires whole numbers |
| `out_of_range` | Outside the event's scale |
| `duplicate` | A second score from the same judge for the same company |
| `unresolved_company`, `unresolved_identity` | Cannot be matched to a company or judge until the operator confirms |
| `rejected` | Excluded by the operator |

**Missing-judge rule** (event setting):

| Policy | When the adjustment applies |
|---|---|
| `one_absent_mean` (default) | Exactly one judge slot is **confirmed** missing (confirmed absent, or confirmed did not score) |
| `mean_for_confirmed_missing` | Any number of confirmed-missing slots (must be explicitly chosen per event) |
| `none` | Never |

Adjusted total = raw sum + (raw sum ÷ number of valid scores) × number of confirmed-missing slots.

Example: five verified 3s plus one confirmed-absent judge gives 15 + 3 = **18 / 30**.

**What never happens:**

- A missing cell, an unresolved name, or an invalid score never triggers the adjustment.
- With no verified individual scores, nothing is calculated, and judge scores are never worked backward from an announced total.

**Display:** the app shows the expected judge count, verified count, individual scores, raw sum, imputed contribution (labeled *imputed*), adjusted total, denominator, announced total and any discrepancy. Full precision is kept internally; display rounding follows the event's "decimals" setting. Ties share a rank and no tiebreaker is applied.

The AI never produces scores or a "readiness score".
