"""Transcription provider adapter (optional).

Supports any OpenAI-compatible ``/audio/transcriptions`` endpoint that can return WebVTT (for example
OpenAI Whisper or a self-hosted whisper server). Audio is extracted locally, split into chunks, sent to
the configured endpoint, and the chunk VTTs are merged with correct offsets into one recording-relative
transcript. Nothing is sent anywhere unless TRANSCRIPTION_PROVIDER and its URL/key are configured and
the operator starts the job.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import httpx

from ..config import get_settings
from ..util import fmt_hms, parse_hms
from .ffmpeg import ffmpeg_exe

CHUNK_SECONDS = 600


class TranscriptionError(Exception):
    pass


def estimate_cost(duration_s: float) -> float:
    return round(duration_s / 60 * get_settings().transcription_price_per_minute, 4)


def describe() -> str:
    s = get_settings()
    if not s.transcription_configured:
        return "Not configured. Upload a transcript, or set TRANSCRIPTION_PROVIDER/URL/KEY to enable transcription."
    host = httpx.URL(s.transcription_api_url).host
    return f"Audio from the recording will be sent to {host} ({s.transcription_model}) for transcription."


def shift_vtt(vtt: str, offset: float) -> list[str]:
    """Return cue blocks of a VTT with times shifted by offset seconds."""
    out = []
    for block in re.split(r"\n\s*\n", vtt.replace("\r\n", "\n").strip()):
        lines = block.split("\n")
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        a, b = [x.strip().split(" ")[0] for x in lines[idx].split("-->")]
        sa, sb = parse_hms(a.replace(",", ".")), parse_hms(b.replace(",", "."))
        if sa is None or sb is None:
            continue
        out.append(f"{fmt_hms(sa + offset, millis=True)} --> {fmt_hms(sb + offset, millis=True)}\n" + "\n".join(lines[idx + 1:]))
    return out


def merge_chunks(chunks: list[tuple[float, str]]) -> str:
    cues = []
    for offset, vtt in chunks:
        cues += shift_vtt(vtt, offset)
    return "WEBVTT\n\n" + "\n\n".join(f"{i}\n{c}" for i, c in enumerate(cues, start=1)) + "\n"


def transcribe(video_path: Path, duration_s: float, progress=lambda *_: None, client: httpx.Client | None = None) -> str:
    s = get_settings()
    if not s.transcription_configured:
        raise TranscriptionError("No transcription provider is configured.")
    chunks = []
    with tempfile.TemporaryDirectory() as td:
        n = max(1, int((duration_s + CHUNK_SECONDS - 1) // CHUNK_SECONDS))
        own = client is None
        client = client or httpx.Client(timeout=600)
        try:
            for i in range(n):
                start = i * CHUNK_SECONDS
                part = Path(td) / f"chunk{i}.mp3"
                subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", "-ss", str(start), "-t", str(CHUNK_SECONDS),
                                "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(part)],
                               capture_output=True, check=True, timeout=1800)
                progress(f"Transcribing part {i + 1} of {n}", 0.05 + 0.9 * i / n)
                with open(part, "rb") as f:
                    r = client.post(s.transcription_api_url, headers={"Authorization": f"Bearer {s.transcription_api_key}"},
                                    data={"model": s.transcription_model, "response_format": "vtt"},
                                    files={"file": (part.name, f, "audio/mpeg")})
                if r.status_code >= 400:
                    raise TranscriptionError(f"Transcription provider returned {r.status_code}.")
                chunks.append((float(start), r.text))
        finally:
            if own:
                client.close()
    return merge_chunks(chunks)
