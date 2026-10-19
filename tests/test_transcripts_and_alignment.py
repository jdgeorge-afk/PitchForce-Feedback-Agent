from pitchforce.analysis import heuristics as H
from pitchforce.media import alignment as AL
from pitchforce.parsing.transcript import parse_transcript, parse_zoom_chat
from pitchforce.parsing.rosters import parse_chat_scores

VTT = """WEBVTT

1
01:18:20.780 --> 01:18:25.000
Jane Host: Next up is Acme with Pat.

2
01:18:25.500 --> 01:18:40.000
Pat Founder: Thanks, we build things.

3
01:18:41.000 --> 01:18:44.000
continuation without a speaker label
"""

SRT = """1
00:00:01,000 --> 00:00:03,500
Pat Founder: Hello there.

2
00:00:04,000 --> 00:00:06,000
Judge Ann: [inaudible] about the market?
"""

TXT = """[Jane Host] 16:18:34
Welcome everyone.

[Pat Founder] 16:18:40
Thanks, we build things
that matter.
"""


def test_vtt_keeps_raw_times_and_labels_and_does_not_guess_speaker():
    p = parse_transcript(VTT, "x.vtt")
    assert p.time_basis == "media"
    assert p.utterances[0].start_raw == "01:18:20.780" and abs(p.utterances[0].start_s - 4700.78) < 1e-6
    assert p.utterances[0].speaker == "Jane Host"
    assert p.utterances[2].speaker == ""  # unlabeled cue stays unlabeled
    assert p.utterances[0].cue_ref == "1"


def test_srt_and_gap_marker():
    p = parse_transcript(SRT, "x.srt")
    assert p.format == "srt" and len(p.utterances) == 2
    assert p.utterances[1].is_gap


def test_txt_wall_clock_multiline():
    p = parse_transcript(TXT, "t.txt")
    assert p.time_basis == "wall_clock"
    assert p.utterances[1].text == "Thanks, we build things that matter."
    assert p.utterances[1].start_raw == "16:18:40" and p.utterances[0].end_s == p.utterances[1].start_s


def test_zoom_chat_scores():
    chat = "16:20:01 From Judge Ann to Everyone: Acme: 4\n16:20:05 From Judge Bob to Everyone: Acme - 3.5\n16:20:09 From Bob to Everyone: great pitch\n"
    assert len(parse_zoom_chat(chat)) == 3
    res = parse_chat_scores(chat.encode())
    assert [(s.company_label, s.judge_label, s.value_raw) for s in res.scores] == [("Acme", "Judge Ann", "4"), ("Acme", "Judge Bob", "3.5")]


def _uvs(lines):
    from pitchforce.parsing.transcript import GAP_RE

    return [H.UV(id=str(i), idx=i, t=t, end=t + 5, raw=str(t), speaker=s, text=x, is_gap=bool(GAP_RE.search(x)))
            for i, (t, s, x) in enumerate(lines)]


def test_incomplete_and_moderator_questions():
    lines = [
        (0, "Host", "Questions from the panel?"),
        (5, "Judge Ann", "How big is the market?"),
        (10, "Pat", "About two billion dollars in the US alone."),
        (20, "Judge Bob", "And if you get acquired what happens to the license, does it"),
        (25, "Pat", "Good question, we're still working on that."),
        (30, "Host", "Any other questions for Pat?"),
        (35, "Judge Ann", "What is the [inaudible] timeline?"),
    ]
    utts = _uvs(lines)
    qs = H.extract_questions(utts, H.Boundary(0, 6, 1, "t"), "Pat", {"Host"}, {"Judge Ann", "Judge Bob"})
    roles = [(q.asker, q.asker_role, q.completeness) for q in qs]
    assert ("Host", "moderator", "incomplete") in roles  # moderator logistics kept separate
    bob = next(q for q in qs if q.asker == "Judge Bob")
    assert bob.completeness == "incomplete" and "cut off" in bob.unresolved
    ann2 = [q for q in qs if q.asker == "Judge Ann"][1]
    assert ann2.completeness == "gap"  # gap shown, not reconstructed


def test_feedback_preserves_nothing_to_add_and_silent_judges():
    lines = [(0, "Host", "Feedback please."), (5, "Judge Ann", "Great traction. You should shorten your answers."),
             (12, "Judge Bob", "Nothing to add.")]
    items = H.extract_feedback(_uvs(lines), H.Boundary(0, 2, 1, "t"), "Pat", {"Host"}, {"Judge Ann", "Judge Bob", "Judge Cy"},
                               ["Judge Ann", "Judge Bob", "Judge Cy"])
    kinds = {(i.speaker, i.kind) for i in items}
    assert ("Judge Bob", "nothing_to_add") in kinds and ("Judge Cy", "silent") in kinds
    assert ("Judge Ann", "positive") in kinds and ("Judge Ann", "recommendation") in kinds
    silent = next(i for i in items if i.kind == "silent")
    assert silent.excerpt == "" and "No feedback" in silent.point


def _pair(n=40, start_wall=50000.0, rec_start=52000.0, edit_at=None, edit_len=0.0, drift=0.0):
    prim, ref = [], []
    for i in range(n):
        t = start_wall + i * 60
        text = f"utterance number {i} with several distinct words alpha beta {i * 7}"
        prim.append(AL.U(t, text))
        if t < rec_start or (edit_at and edit_at <= t < edit_at + edit_len):
            continue
        m = t - rec_start - (edit_len if edit_at and t >= edit_at + edit_len else 0)
        m += drift * (t - rec_start)
        ref.append(AL.U(m, text))
    return prim, ref


def test_alignment_offset_never_uses_first_timestamp():
    prim, ref = _pair()
    a = AL.anchor_match(prim, ref)
    assert a.status == "ok"
    m, q = a.to_media(52300.0)
    assert q == "mapped" and abs(m - 300.0) < 0.5  # 52300 - recording start 52000, not 52300 - first stamp 50000


def test_alignment_handles_edit_and_drift():
    prim, ref = _pair(n=100, edit_at=53800.0, edit_len=120.0)
    a = AL.anchor_match(prim, ref)
    assert len(a.pieces) == 2 and a.status in ("ok", "needs_review")
    assert abs(a.to_media(53500.0)[0] - 1500.0) < 0.5
    assert abs(a.to_media(54500.0)[0] - (54500 - 52000 - 120)) < 0.5
    prim, ref = _pair(n=100, drift=0.001)
    d = AL.anchor_match(prim, ref)
    assert d.drift_detected
    assert abs(d.to_media(54000.0)[0] - (2000 * 1.001)) < 0.6


def test_operator_offset_and_identity_checks():
    a = AL.operator_offset("15:00:14", [AL.U(14 * 3600 + 20 * 60 + 33, "x"), AL.U(16 * 3600, "y")])
    assert abs(a.to_media(16 * 3600)[0] - (16 * 3600 - (15 * 3600 + 14))) < 1e-6
    assert any("before the recording began" in n for n in a.notes)
    ident = AL.identity(600.0, [AL.U(10, "a"), AL.U(5000, "b")])
    assert ident.status == "needs_review"  # transcript longer than the video
