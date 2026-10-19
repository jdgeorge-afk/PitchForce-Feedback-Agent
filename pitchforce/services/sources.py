"""Source-file registration and validation (shared by uploads, sample mode, and tests)."""
from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Event, SourceFile
from ..storage import get_storage

KIND_EXTENSIONS = {
    "video": None,  # from settings
    "transcript": {".txt", ".vtt", ".srt"},
    "company_roster": {".xlsx", ".csv"},
    "judge_roster": {".xlsx", ".csv"},
    "interest": {".xlsx", ".csv"},
    "scores": {".xlsx", ".csv"},
    "chat": {".txt"},
    "deck": {".pdf", ".pptx", ".docx"},
    "factsheet": {".pdf", ".docx", ".txt", ".md"},
    "guidelines": {".pdf", ".docx", ".txt", ".md", ".zip"},
    "other": {".pdf", ".docx", ".txt", ".md", ".csv", ".xlsx"},
}

KIND_LABELS = {
    "video": "Event recording (video)",
    "transcript": "Transcript",
    "company_roster": "Company / presenter roster",
    "judge_roster": "Investor / judge roster",
    "interest": "Investment-interest poll export",
    "scores": "Judge scores",
    "chat": "Zoom chat export (scores)",
    "deck": "Pitch deck",
    "factsheet": "Company fact sheet",
    "guidelines": "Pitch guidelines / best practices",
    "other": "Other reference file",
}

MAGIC = {
    ".xlsx": [b"PK\x03\x04"], ".docx": [b"PK\x03\x04"], ".pptx": [b"PK\x03\x04"], ".zip": [b"PK\x03\x04"],
    ".pdf": [b"%PDF"],
}
VIDEO_MAGIC_OFFSET4 = [b"ftyp"]  # mp4/mov/m4v
VIDEO_MAGIC_START = [b"\x1a\x45\xdf\xa3", b"RIFF"]  # mkv/webm, avi


class SourceValidationError(ValueError):
    pass


def allowed_extensions(kind: str) -> set[str]:
    if kind == "video":
        return get_settings().video_exts
    return KIND_EXTENSIONS.get(kind, set()) or set()


def validate_name_and_size(kind: str, filename: str, size: int) -> None:
    s = get_settings()
    if kind not in KIND_EXTENSIONS:
        raise SourceValidationError(f"Unknown file type category: {kind}")
    ext = Path(filename).suffix.lower()
    allowed = allowed_extensions(kind)
    if ext not in allowed:
        raise SourceValidationError(f"{KIND_LABELS[kind]} must be one of: {', '.join(sorted(allowed))} (got {ext or 'no extension'}).")
    limit_mb = s.max_upload_mb if kind == "video" else s.max_document_upload_mb
    if size > limit_mb * 1024 * 1024:
        raise SourceValidationError(f"File is larger than the {limit_mb} MB limit for this file type.")
    if size <= 0:
        raise SourceValidationError("File is empty.")


def validate_content(kind: str, filename: str, head: bytes) -> None:
    ext = Path(filename).suffix.lower()
    if kind == "video":
        if not (head[4:8] in VIDEO_MAGIC_OFFSET4 or any(head.startswith(m) for m in VIDEO_MAGIC_START)):
            raise SourceValidationError("This does not look like a video file (MP4/MOV/MKV/WEBM/AVI).")
        return
    if ext in MAGIC and not any(head.startswith(m) for m in MAGIC[ext]):
        raise SourceValidationError(f"The file content does not match its {ext} extension.")
    if ext in (".txt", ".vtt", ".srt", ".csv", ".md"):
        if b"\x00" in head[:4096] and not head.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise SourceValidationError("This text file contains binary data.")


def register_file(db: Session, event: Event | None, kind: str, filename: str, path: Path, user_id: str | None = None,
                  move: bool = False) -> SourceFile:
    """Validate, store privately, and record a source file. Re-uploading identical content returns the existing record."""
    size = path.stat().st_size
    validate_name_and_size(kind, filename, size)
    with open(path, "rb") as f:
        head = f.read(8192)
    validate_content(kind, filename, head)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    digest = h.hexdigest()
    existing = db.scalar(select(SourceFile).where(SourceFile.event_id == (event.id if event else None),
                                                  SourceFile.sha256 == digest, SourceFile.kind == kind,
                                                  SourceFile.status != "superseded"))
    if existing:
        if move:
            path.unlink(missing_ok=True)
        return existing
    st = get_storage()
    key = st.make_key(f"sources/{event.id if event else 'global'}", Path(filename).suffix)
    st.put_file(key, path, move=move)
    src = SourceFile(event_id=event.id if event else None, kind=kind, original_filename=Path(filename).name[:500],
                     storage_key=key, size=size, sha256=digest, uploaded_by=user_id, status="uploaded")
    db.add(src)
    db.flush()
    return src


def register_bytes(db: Session, event: Event | None, kind: str, filename: str, data: bytes, user_id: str | None = None) -> SourceFile:
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(filename).suffix) as tmp:
        tmp.write(data)
        p = Path(tmp.name)
    try:
        return register_file(db, event, kind, filename, p, user_id, move=True)
    finally:
        p.unlink(missing_ok=True)
