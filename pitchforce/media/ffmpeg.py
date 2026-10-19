"""FFmpeg wrapper.

All invocations use argument lists (no shell) and application-generated paths, so untrusted
filenames are never interpreted by a shell. The binary is the one bundled by imageio-ffmpeg unless
FFMPEG_PATH is set.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import get_settings


class MediaError(Exception):
    pass


def ffmpeg_exe() -> str:
    s = get_settings()
    if s.ffmpeg_path:
        return s.ffmpeg_path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # pragma: no cover - depends on environment
        raise MediaError("FFmpeg is not available. Install it or set FFMPEG_PATH.") from e


@dataclass
class ProbeResult:
    duration_s: float | None
    has_video: bool
    has_audio: bool
    video_codec: str = ""
    audio_codec: str = ""
    width: int | None = None
    height: int | None = None
    container: str = ""
    streams: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


DUR_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
STREAM_RE = re.compile(r"Stream #\d+:\d+(?:\[[^\]]*\])?(?:\([^)]*\))?:\s*(Video|Audio|Subtitle|Data):\s*([^\s,]+)(.*)")
INPUT_RE = re.compile(r"Input #0,\s*([^,]+(?:,[^,]+)*?),\s*from")


def probe(path: Path) -> ProbeResult:
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    err = proc.stderr
    if "Invalid data found" in err or "could not find codec" in err.lower() or "moov atom not found" in err:
        raise MediaError("The file is not a readable video (it may be incomplete or not a video).")
    d = DUR_RE.search(err)
    dur = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else None
    res = ProbeResult(duration_s=dur, has_video=False, has_audio=False)
    m = INPUT_RE.search(err)
    if m:
        res.container = m.group(1)
    for sm in STREAM_RE.finditer(err):
        kind, codec, rest = sm.group(1), sm.group(2), sm.group(3)
        res.streams.append(f"{kind}: {codec}")
        if kind == "Video" and not res.has_video and "attached pic" not in rest:
            res.has_video = True
            res.video_codec = codec
            wh = re.search(r"\b(\d{2,5})x(\d{2,5})\b", rest)
            if wh:
                res.width, res.height = int(wh.group(1)), int(wh.group(2))
        elif kind == "Audio" and not res.has_audio:
            res.has_audio = True
            res.audio_codec = codec
    if not res.has_video and not res.has_audio:
        raise MediaError("No audio or video streams were found in this file.")
    return res


TIME_RE = re.compile(r"out_time_(?:ms|us)=(\d+)")


def render_clip(src: Path, dst: Path, start_s: float, end_s: float,
                on_progress: Callable[[float], None] | None = None,
                should_cancel: Callable[[], bool] | None = None) -> None:
    """Cut [start_s, end_s] from src into an H.264/AAC MP4 (frame-accurate re-encode)."""
    if end_s <= start_s:
        raise MediaError("Clip end must be after clip start.")
    dur = end_s - start_s
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".rendering.mp4")
    args = [
        ffmpeg_exe(), "-hide_banner", "-nostdin", "-y",
        "-ss", f"{start_s:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
        "-map", "0:v:0?", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        str(tmp),
    ]
    timeout = get_settings().clip_render_timeout_s
    import tempfile

    errlog = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")  # never a pipe: avoids stderr deadlock
    proc = subprocess.Popen(args[:3] + ["-loglevel", "error"] + args[3:], stdout=subprocess.PIPE, stderr=errlog, text=True,
                            encoding="utf-8", errors="replace")
    started = time.monotonic()
    last_report = 0.0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            m = TIME_RE.search(line)
            if m and on_progress:
                done = int(m.group(1)) / 1_000_000
                if time.monotonic() - last_report > 1.0:
                    on_progress(min(1.0, done / dur))
                    last_report = time.monotonic()
            if should_cancel and should_cancel():
                proc.kill()
                raise MediaError("cancelled")
            if time.monotonic() - started > timeout:
                proc.kill()
                raise MediaError("Rendering timed out.")
        proc.wait(timeout=60)
        errlog.seek(0)
        stderr = errlog.read()
        if proc.returncode != 0:
            raise MediaError(f"FFmpeg failed (exit {proc.returncode}): {stderr.strip().splitlines()[-1] if stderr.strip() else ''}")
        tmp.replace(dst)
    finally:
        if proc.poll() is None:
            proc.kill()
        errlog.close()
        if tmp.exists() and not dst.exists():
            tmp.unlink(missing_ok=True)


def extract_frame(src: Path, at_s: float, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", "-ss", f"{max(0, at_s):.3f}", "-i", str(src),
                    "-frames:v", "1", "-vf", "scale=320:-2", str(dst)], capture_output=True, timeout=120)


def extract_audio(src: Path, dst: Path) -> None:
    """Mono 16 kHz MP3 for transcription providers (smaller upload)."""
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                    "-b:a", "48k", str(dst)], capture_output=True, timeout=3600, check=True)


def make_test_video(dst: Path, seconds: float, size: str = "320x180", rate: int = 10) -> None:
    """Synthetic test pattern with a tone. Used for sample mode and tests only."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = [
        ffmpeg_exe(), "-hide_banner", "-nostdin", "-y",
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050",
        "-t", f"{seconds:.3f}", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "48k", "-shortest", str(dst),
    ]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise MediaError(proc.stderr[-400:])
