"""Transcript-to-video time alignment.

A transcript's timestamps are mapped onto the recording's media time with a piecewise-linear map:

    media_t = t + offset + rate * (t - t_from)     for t in [t_from, t_to]

Sources of evidence, in order of preference:
1. A recording-relative transcript (VTT/SRT from the recording itself) is media time already
   (``identity_media``), checked against the video duration.
2. Matched utterances between a wall-clock transcript and a recording-relative transcript
   (``anchor_match``). Many anchors across the recording are used so pauses, missing pre-roll and
   edited-out sections show up as separate pieces, and drift shows up as a non-zero rate.
3. An operator-supplied recording start time (``operator_offset``) - single anchor, drift unknown.
4. Manual anchors entered by the operator (``manual``).

The first timestamp of an uploaded transcript is never assumed to equal video time zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from rapidfuzz import fuzz

from ..util import fmt_hms, norm_name, parse_hms


@dataclass
class Piece:
    t_from: float
    t_to: float
    offset: float
    rate: float = 0.0
    anchors: int = 0

    def apply(self, t: float) -> float:
        return t + self.offset + self.rate * (t - self.t_from)


@dataclass
class Alignment:
    method: str
    pieces: list[Piece] = field(default_factory=list)
    anchors: list[dict] = field(default_factory=list)
    residual_max_s: float | None = None
    drift_detected: bool = False
    status: str = "needs_review"
    notes: list[str] = field(default_factory=list)

    def to_media(self, t: float | None) -> tuple[float | None, str]:
        """Map a transcript time to media time. Returns (media_t, quality) where quality is
        'mapped', 'extrapolated' (outside anchored range, nearest piece used) or 'unmapped'."""
        if t is None or not self.pieces:
            return None, "unmapped"
        for p in self.pieces:
            if p.t_from - 1e-6 <= t <= p.t_to + 1e-6:
                return p.apply(t), "mapped"
        # Between pieces: a section absent from the recording (edited out / paused).
        before = [p for p in self.pieces if p.t_to < t]
        after = [p for p in self.pieces if p.t_from > t]
        if before and after:
            return after[0].apply(after[0].t_from), "gap"
        p = before[-1] if before else after[0]
        return p.apply(t), "extrapolated"

    def as_json(self) -> dict:
        return {
            "method": self.method,
            "pieces": [p.__dict__ for p in self.pieces],
            "anchors": self.anchors,
            "residual_max_s": self.residual_max_s,
            "drift_detected": self.drift_detected,
            "status": self.status,
            "notes": self.notes,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Alignment":
        a = cls(method=d.get("method", ""), anchors=d.get("anchors", []), residual_max_s=d.get("residual_max_s"),
                drift_detected=d.get("drift_detected", False), status=d.get("status", "needs_review"),
                notes=d.get("notes", []))
        a.pieces = [Piece(**p) for p in d.get("pieces", [])]
        return a


@dataclass
class U:
    """Minimal utterance view used by the aligner."""
    t: float | None
    text: str
    speaker: str = ""
    ref: str = ""


def identity(video_duration: float | None, transcript: list[U]) -> Alignment:
    times = [u.t for u in transcript if u.t is not None]
    a = Alignment(method="identity_media")
    if not times:
        a.status = "failed"
        a.notes.append("Transcript has no timestamps.")
        return a
    a.pieces = [Piece(min(times), max(times) + 60, 0.0, 0.0, len(times))]
    a.residual_max_s = 0.0
    if video_duration is not None and max(times) > video_duration + 5:
        a.status = "needs_review"
        a.notes.append(
            f"Transcript runs to {fmt_hms(max(times))} but the video is only {fmt_hms(video_duration)} long; "
            "this transcript may not belong to this recording."
        )
    else:
        a.status = "ok"
        a.notes.append("Recording-relative transcript used directly as video time.")
    return a


def operator_offset(recording_start_wallclock: str, transcript: list[U]) -> Alignment:
    start = parse_hms(recording_start_wallclock)
    a = Alignment(method="operator_offset")
    if start is None:
        a.status = "failed"
        a.notes.append(f"Recording start time {recording_start_wallclock!r} is not a valid HH:MM:SS time.")
        return a
    times = [u.t for u in transcript if u.t is not None]
    if not times:
        a.status = "failed"
        a.notes.append("Transcript has no timestamps.")
        return a
    t0 = min(times)
    if t0 < start - 12 * 3600:  # transcript crosses midnight relative to start (rare)
        start -= 24 * 3600
    a.pieces = [Piece(min(times), max(times) + 60, -start, 0.0, 1)]
    a.anchors = [{"t": start, "media": 0.0, "source": "operator: recording started at " + recording_start_wallclock}]
    a.status = "ok"
    a.notes.append(
        f"Operator stated the recording began at {recording_start_wallclock}. Single anchor: pauses or edits inside "
        "the recording cannot be detected this way; check clip boundaries in the player."
    )
    if t0 < start:
        a.notes.append(
            f"The transcript starts at {fmt_hms(t0)}, before the recording began; content before {recording_start_wallclock} "
            "is not in the video."
        )
    return a


def _similar(a: str, b: str) -> float:
    """Similarity of two utterances, emphasising how they *start* (so cue splitting between
    transcripts still yields aligned start times)."""
    na, nb = norm_name(a), norm_name(b)
    whole = fuzz.ratio(na, nb)
    wa, wb = na.split(), nb.split()
    if len(wa) >= 6 and len(wb) >= 6:
        head = fuzz.ratio(" ".join(wa[:10]), " ".join(wb[:10]))
        return max(whole, head)
    return whole


def anchor_match(primary: list[U], reference: list[U], min_words: int = 6, min_sim: float = 88.0,
                 jump_threshold_s: float = 3.0) -> Alignment:
    """Align a transcript (any basis) to a recording-relative reference transcript by matching text."""
    a = Alignment(method="anchor_match")
    ref = [r for r in reference if r.t is not None and len(r.text.split()) >= min_words]
    cands = [p for p in primary if p.t is not None and len(p.text.split()) >= min_words]
    if not ref or not cands:
        a.status = "failed"
        a.notes.append("Not enough timed text to match between transcripts.")
        return a
    # Index reference by first significant word for speed.
    anchors = []
    used_ref = set()
    for p in cands:
        best, best_s, second = None, 0.0, 0.0
        for i, r in enumerate(ref):
            if i in used_ref:
                continue
            s = _similar(p.text, r.text)
            if s > best_s:
                best, best_s, second = i, s, best_s
            elif s > second:
                second = s
        # Accept only distinctive matches: clearly better than the next-best passage.
        if best is not None and best_s >= min_sim and best_s - second >= 3:
            used_ref.add(best)
            anchors.append((p.t, ref[best].t, best_s, p.ref, ref[best].ref))
    if len(anchors) < 2:
        a.status = "failed"
        a.notes.append(f"Only {len(anchors)} matching passage(s) found; cannot align reliably.")
        a.anchors = [{"t": t, "media": m, "sim": s, "ref": pr, "media_ref": rr} for t, m, s, pr, rr in anchors]
        return a
    anchors.sort()
    # Remove outliers: anchors whose offset disagrees with both neighbours.
    offs = [m - t for t, m, *_ in anchors]
    keep = []
    for i, (t, m, s, pr, rr) in enumerate(anchors):
        neigh = [offs[j] for j in (i - 1, i + 1) if 0 <= j < len(offs)]
        if neigh and all(abs(offs[i] - n) > jump_threshold_s for n in neigh) and len(anchors) > 3:
            a.notes.append(f"Ignored inconsistent match at {fmt_hms(t)} (offset {offs[i]:+.1f}s).")
            continue
        keep.append((t, m, s, pr, rr))
    anchors = keep
    a.anchors = [{"t": t, "media": m, "offset": m - t, "sim": round(s, 1), "ref": pr, "media_ref": rr} for t, m, s, pr, rr in anchors]
    # Split into pieces where the offset jumps (edits/pauses in the recording).
    groups: list[list[tuple]] = [[anchors[0]]]
    for prev, cur in zip(anchors, anchors[1:]):
        if abs((cur[1] - cur[0]) - (prev[1] - prev[0])) > jump_threshold_s:
            groups.append([cur])
        else:
            groups[-1].append(cur)
    pieces = []
    residual = 0.0
    for gi, g in enumerate(groups):
        ts = [x[0] for x in g]
        os_ = [x[1] - x[0] for x in g]
        rate = 0.0
        if len(g) >= 3 and ts[-1] - ts[0] > 60:
            # least-squares slope of offset vs time
            mt, mo = sum(ts) / len(ts), sum(os_) / len(os_)
            den = sum((t - mt) ** 2 for t in ts)
            rate = sum((t - mt) * (o - mo) for t, o in zip(ts, os_)) / den if den else 0.0
            if abs(rate) < 1e-4:
                rate = 0.0
        off0 = median([o - rate * (t - ts[0]) for t, o in zip(ts, os_)])
        t_from = ts[0] if gi > 0 else min(ts[0], min(u.t for u in cands))
        t_to = groups[gi + 1][0][0] - 0.001 if gi + 1 < len(groups) else max(u.t for u in primary if u.t is not None) + 60
        if gi > 0:
            # boundary lies somewhere between last anchor of previous group and first of this one
            t_from = (groups[gi - 1][-1][0] + ts[0]) / 2
            pieces[-1].t_to = t_from - 0.001
        piece = Piece(t_from, t_to, off0 + rate * (t_from - ts[0]), rate, len(g))
        pieces.append(piece)
        for t, o in zip(ts, os_):
            residual = max(residual, abs(piece.apply(t) - (t + o)))
        if abs(rate) * 600 > 0.5:
            a.drift_detected = True
    a.pieces = pieces
    a.residual_max_s = round(residual, 3)
    # Coverage is measured on the recording side: anchors should spread across the recording.
    media_ts = [m for _, m, *_ in anchors]
    ref_span = max(r.t for r in ref) - min(r.t for r in ref)
    coverage = (max(media_ts) - min(media_ts)) / ref_span if ref_span > 0 else 0
    if len(groups) > 1:
        a.notes.append(
            f"The recording has {len(groups) - 1} discontinuit{'y' if len(groups) == 2 else 'ies'} relative to the transcript "
            "(pause, edit, or missing section). Each part is mapped separately."
        )
    if a.drift_detected:
        a.notes.append("Gradual drift between transcript and recording detected and corrected.")
    a.status = "ok" if (len(anchors) >= 3 and coverage >= 0.5 and residual <= 2.0) else "needs_review"
    if a.status != "ok":
        a.notes.append(f"Alignment confidence is limited ({len(anchors)} anchors, {coverage:.0%} coverage, max residual {residual:.1f}s).")
    return a


def manual(anchor_pairs: list[tuple[float, float]], transcript: list[U]) -> Alignment:
    """Operator-entered anchors (transcript time -> media time)."""
    a = Alignment(method="manual")
    if not anchor_pairs:
        a.status = "failed"
        return a
    pairs = sorted(anchor_pairs)
    times = [u.t for u in transcript if u.t is not None] or [pairs[0][0]]
    pieces = []
    for i, (t, m) in enumerate(pairs):
        t_from = min(times) if i == 0 else t
        t_to = pairs[i + 1][0] - 0.001 if i + 1 < len(pairs) else max(times) + 60
        pieces.append(Piece(t_from, t_to, m - t, 0.0, 1))
    a.pieces = pieces
    a.anchors = [{"t": t, "media": m, "source": "operator"} for t, m in pairs]
    a.status = "confirmed"
    return a
