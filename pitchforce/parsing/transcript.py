"""Transcript parsing: WebVTT, SRT, and common plain-text transcript layouts.

Original timestamps and speaker labels are kept verbatim. Seconds are computed in the source's own
time basis:
- ``media``: recording-relative (VTT/SRT, or text with 00:mm:ss.mmm style stamps)
- ``wall_clock``: time of day (e.g. Zoom TXT "16:18:34")
The first timestamp is never assumed to be video time zero; mapping to video time happens in
``media/alignment.py``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..util import parse_hms

GAP_RE = re.compile(
    r"\[(?:inaudible|crosstalk|no audio|audio (?:cut|drop)[^\]]*|silence|unintelligible|indistinct|gap)[^\]]*\]"
    r"|\((?:inaudible|crosstalk|unintelligible|indistinct)[^)]*\)",
    re.I,
)
ARROW = re.compile(
    r"^\s*(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}|\d{1,2}:\d{2}:\d{2})\s*-->\s*(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}|\d{1,2}:\d{2}:\d{2})"
)
SPEAKER_PREFIX = re.compile(r"^(?:<v\s+([^>]+)>|([A-Z][^:\n]{0,60}?)\s*:\s+)(.*)$", re.S)

TXT_BRACKET_HEADER = re.compile(r"^\s*\[([^\]]+)\]\s+(\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,3})?)\s*$")
TXT_NAME_TIME_HEADER = re.compile(r"^\s*([^\d\[\]:][^\[\]]{0,80}?)\s{2,}(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)\s*$")
TXT_TIME_NAME_TEXT = re.compile(
    r"^\s*(\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,3})?)\s+(?:From\s+)?([^:]{1,80}?)\s*:\s?(.*)$"
)
TXT_TIME_ONLY_LINE = re.compile(r"^\s*\(?(\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,3})?)\)?\s*$")
TXT_NAME_TEXT = re.compile(r"^\s*([A-Z][\w .'’\-()]{0,60}?)\s*:\s+(.+)$")


@dataclass
class ParsedUtterance:
    seq: int
    cue_ref: str
    speaker: str
    start_raw: str
    end_raw: str
    start_s: float | None
    end_s: float | None
    text: str
    is_gap: bool = False


@dataclass
class TranscriptParse:
    format: str
    time_basis: str  # media | wall_clock | none
    utterances: list[ParsedUtterance] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def speakers(self) -> list[str]:
        seen: dict[str, None] = {}
        for u in self.utterances:
            if u.speaker:
                seen.setdefault(u.speaker, None)
        return list(seen)


def _norm_ts(raw: str) -> float | None:
    return parse_hms(raw.replace(",", "."))


def _split_speaker(text: str) -> tuple[str, str]:
    m = SPEAKER_PREFIX.match(text)
    if not m:
        return "", text
    speaker = (m.group(1) or m.group(2) or "").strip()
    body = m.group(3).strip()
    # Avoid treating "Note: something" style sentences as speakers when they look like prose.
    if m.group(2) and (len(speaker.split()) > 6 or speaker.lower() in {"note", "question", "answer", "q", "a", "http", "https"}):
        return "", text
    if m.group(1):
        body = re.sub(r"</v>\s*$", "", body)
    return speaker, body


def _fix_midnight(utts: list[ParsedUtterance]) -> None:
    """Wall-clock transcripts that cross midnight: keep times monotonic."""
    offset = 0.0
    prev = None
    for u in utts:
        if u.start_s is None:
            continue
        if prev is not None and u.start_s + offset < prev - 12 * 3600:
            offset += 24 * 3600
        u.start_s += offset
        if u.end_s is not None:
            u.end_s += offset
        prev = u.start_s


def parse_vtt_srt(text: str, fmt: str) -> TranscriptParse:
    out = TranscriptParse(format=fmt, time_basis="media")
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    seq = 0
    last_speaker = ""
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue
        if lines[0].strip().upper().startswith("WEBVTT") or lines[0].startswith("NOTE") or lines[0].startswith("STYLE"):
            continue
        cue_ref = ""
        idx = 0
        if not ARROW.match(lines[0]) and len(lines) > 1 and ARROW.match(lines[1]):
            cue_ref = lines[0].strip()
            idx = 1
        m = ARROW.match(lines[idx]) if idx < len(lines) else None
        if not m:
            out.warnings.append(f"Skipped unrecognized block: {lines[0][:60]!r}")
            continue
        body = " ".join(ln.strip() for ln in lines[idx + 1 :])
        speaker, utt_text = _split_speaker(body)
        if not speaker:
            speaker = ""  # do not guess; continuation cues stay unattributed
        else:
            last_speaker = speaker
        seq += 1
        out.utterances.append(
            ParsedUtterance(
                seq=seq,
                cue_ref=cue_ref or str(seq),
                speaker=speaker,
                start_raw=m.group(1),
                end_raw=m.group(2),
                start_s=_norm_ts(m.group(1)),
                end_s=_norm_ts(m.group(2)),
                text=utt_text,
                is_gap=bool(GAP_RE.search(utt_text)) or not utt_text.strip(),
            )
        )
    _ = last_speaker
    return out


def _guess_basis(stamps: list[str]) -> str:
    vals = [_norm_ts(s) for s in stamps if _norm_ts(s) is not None]
    if not vals:
        return "none"
    if any(re.search(r"[.,]\d{1,3}$", s) for s in stamps[:5]):
        return "media"
    if vals[0] < 600:  # starts within the first ten minutes: almost certainly recording-relative
        return "media"
    return "wall_clock"


def parse_txt(text: str) -> TranscriptParse:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    utts: list[ParsedUtterance] = []
    stamps: list[str] = []
    cur: dict | None = None
    layout = "unknown"

    def flush():
        nonlocal cur
        if cur and (cur["text"].strip() or cur["speaker"]):
            body = cur["text"].strip()
            utts.append(
                ParsedUtterance(
                    seq=len(utts) + 1,
                    cue_ref=f"line {cur['line']}",
                    speaker=cur["speaker"],
                    start_raw=cur["ts"],
                    end_raw="",
                    start_s=_norm_ts(cur["ts"]) if cur["ts"] else None,
                    end_s=None,
                    text=body,
                    is_gap=bool(GAP_RE.search(body)) or not body,
                )
            )
        cur = None

    pending_ts = ""
    for i, raw in enumerate(lines, start=1):
        line = raw.rstrip()
        if not line.strip():
            continue
        m = TXT_BRACKET_HEADER.match(line)
        if m:
            flush()
            layout = "bracket_header"
            cur = {"speaker": m.group(1).strip(), "ts": m.group(2), "text": "", "line": i}
            stamps.append(m.group(2))
            continue
        m = TXT_TIME_NAME_TEXT.match(line)
        if m and layout in ("unknown", "time_name_text"):
            flush()
            layout = "time_name_text"
            speaker = re.sub(r"\s+to\s+(Everyone|Me|Host).*$", "", m.group(2).strip(), flags=re.I)
            cur = {"speaker": speaker, "ts": m.group(1), "text": m.group(3), "line": i}
            stamps.append(m.group(1))
            continue
        m = TXT_NAME_TIME_HEADER.match(line)
        if m and layout in ("unknown", "name_time_header"):
            flush()
            layout = "name_time_header"
            cur = {"speaker": m.group(1).strip(), "ts": m.group(2), "text": "", "line": i}
            stamps.append(m.group(2))
            continue
        m = TXT_TIME_ONLY_LINE.match(line)
        if m:
            flush()
            pending_ts = m.group(1)
            stamps.append(pending_ts)
            continue
        m = TXT_NAME_TEXT.match(line)
        if m and layout in ("unknown", "name_text") and (cur is None or cur.get("text")):
            flush()
            layout = "name_text" if layout == "unknown" else layout
            cur = {"speaker": m.group(1).strip(), "ts": pending_ts, "text": m.group(2), "line": i}
            pending_ts = ""
            continue
        if cur is None:
            cur = {"speaker": "", "ts": pending_ts, "text": "", "line": i}
            pending_ts = ""
        cur["text"] = (cur["text"] + " " + line.strip()).strip()
    flush()
    out = TranscriptParse(format=f"txt:{layout}", time_basis=_guess_basis(stamps), utterances=utts)
    if out.time_basis == "wall_clock":
        _fix_midnight(out.utterances)
    # Derive end times from the next utterance start (text transcripts carry only start times).
    for a, b in zip(out.utterances, out.utterances[1:]):
        if a.start_s is not None and b.start_s is not None and b.start_s >= a.start_s:
            a.end_s = b.start_s
    if not utts:
        out.warnings.append("No utterances recognized in this text file.")
    elif not stamps:
        out.warnings.append("This transcript has no timestamps; clip boundaries cannot be derived from it.")
    return out


def parse_transcript(content: bytes | str, filename: str = "") -> TranscriptParse:
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    head = text.lstrip()[:200].upper()
    name = filename.lower()
    if head.startswith("WEBVTT") or name.endswith(".vtt"):
        return parse_vtt_srt(text, "vtt")
    if name.endswith(".srt") or re.match(r"^\s*\d+\s*\n\s*\d{1,2}:\d{2}:\d{2},\d{3}\s*-->", text):
        return parse_vtt_srt(text, "srt")
    return parse_txt(text)


# ---------------------------------------------------------------------------
# Zoom chat export (used for judge scores typed into chat)
# ---------------------------------------------------------------------------
CHAT_LINE = re.compile(
    r"^\s*(\d{1,2}:\d{2}:\d{2})\s+(?:From\s+)?(.+?)(?:\s+to\s+(.+?))?\s*:\s*(.*)$", re.I
)


@dataclass
class ChatMessage:
    line: int
    ts_raw: str
    sender: str
    recipient: str
    text: str


def parse_zoom_chat(content: bytes | str) -> list[ChatMessage]:
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    msgs: list[ChatMessage] = []
    for i, line in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        if not line.strip():
            continue
        m = CHAT_LINE.match(line)
        if m:
            msgs.append(ChatMessage(i, m.group(1), m.group(2).strip(), (m.group(3) or "").strip(), m.group(4).strip()))
        elif msgs and line.startswith(("\t", " ")):
            msgs[-1].text = (msgs[-1].text + " " + line.strip()).strip()
    return msgs
