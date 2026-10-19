"""Small shared helpers: text normalization, hashing, timestamps, sanitization."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from typing import Any

_PUNCT_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "–": "-", "—": "-", "−": "-", " ": " ", "…": "...",
    "﻿": "", "​": "",
}


def normalize_punct(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    for k, v in _PUNCT_MAP.items():
        s = s.replace(k, v)
    return s


def norm_name(s: str | None) -> str:
    """Lowercase, punctuation-folded, whitespace-collapsed key used for *comparison only*."""
    if not s:
        return ""
    s = normalize_punct(s).lower()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9@.+ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_company(s: str | None) -> str:
    s = norm_name(s)
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|plc|gmbh|bio|the)\b\.?", " ", s)
    return re.sub(r"\s+", "", s)


def norm_email(s: str | None) -> str:
    if not s:
        return ""
    s = normalize_punct(s).strip().strip("<>").lower()
    return s if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s) else ""


def stable_hash(obj: Any) -> str:
    data = json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


_TS = re.compile(r"^\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?\s*$")


def parse_hms(s: str) -> float | None:
    """'01:18:20.780' / '16:18:34' / '05:03' -> seconds."""
    m = _TS.match(s or "")
    if not m:
        return None
    h = int(m.group(1) or 0)
    mi = int(m.group(2))
    se = int(m.group(3))
    frac = m.group(4)
    ms = int(frac.ljust(3, "0")) if frac else 0
    if mi >= 60 or se >= 60:
        return None
    return h * 3600 + mi * 60 + se + ms / 1000.0


def fmt_hms(seconds: float | None, millis: bool = False) -> str:
    if seconds is None:
        return "—"
    neg = seconds < 0
    seconds = abs(seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    out = f"{h:02d}:{m:02d}:{s:06.3f}" if millis else f"{h:02d}:{m:02d}:{int(s):02d}"
    return ("-" if neg else "") + out


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(round(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")


def excel_safe(value: Any) -> Any:
    """Neutralize spreadsheet formula injection for text cells (OWASP CSV injection guidance)."""
    if isinstance(value, str) and value and value.startswith(FORMULA_PREFIXES):
        # Allow plain negative numbers stored as text? They are converted to numbers by callers instead.
        return "'" + value
    return value


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(s: str | None, max_len: int | None = None) -> str:
    """Strip control characters from uploaded text before rendering into documents."""
    if not s:
        return ""
    s = _CTRL.sub("", str(s))
    if max_len and len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def safe_filename(name: str, default: str = "file") -> str:
    name = normalize_punct(name or "")
    name = re.sub(r"[^A-Za-z0-9._ -]+", "", name).strip(" .")
    name = re.sub(r"\s+", "_", name)
    return name[:120] or default


def now_local_str(dt: datetime | None = None) -> str:
    return (dt or datetime.now()).strftime("%Y-%m-%d %H:%M")


def word_count(s: str) -> int:
    return len(re.findall(r"\b\w[\w'’-]*\b", s or ""))
