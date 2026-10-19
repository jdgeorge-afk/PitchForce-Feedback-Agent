"""Text extraction from supporting documents and safe archive handling."""
from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..config import get_settings


class ArchiveError(Exception):
    pass


@dataclass
class ArchiveMember:
    path: str
    data: bytes
    sha256: str
    archived: bool  # under an "Old"/"Archive" folder


@dataclass
class ArchiveResult:
    members: list[ArchiveMember] = field(default_factory=list)
    duplicates: list[tuple[str, str]] = field(default_factory=list)  # (path, duplicate_of)
    skipped: list[str] = field(default_factory=list)


ARCHIVE_DIR_NAMES = {"old", "archive", "archived", "previous", "outdated", "_old", "backup"}


def safe_extract_zip(data: bytes, allowed_ext: set[str] | None = None) -> ArchiveResult:
    """Read a ZIP in memory with traversal, file-count, size, and compression-ratio limits.
    Nothing is written to disk by this function."""
    s = get_settings()
    out = ArchiveResult()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ArchiveError("Not a valid ZIP archive.") from e
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > s.zip_max_files:
        raise ArchiveError(f"Archive has {len(infos)} files; the limit is {s.zip_max_files}.")
    total = 0
    seen: dict[str, str] = {}
    for info in infos:
        name = info.filename.replace("\\", "/")
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or name.startswith("/") or ":" in p.parts[0]:
            out.skipped.append(f"{name} (unsafe path)")
            continue
        if any(part.startswith("__MACOSX") or part.startswith(".") for part in p.parts):
            out.skipped.append(f"{name} (system file)")
            continue
        if allowed_ext and p.suffix.lower() not in allowed_ext:
            out.skipped.append(f"{name} (type not used)")
            continue
        if info.file_size > 0 and info.compress_size > 0 and info.file_size / info.compress_size > s.zip_max_ratio:
            raise ArchiveError(f"{name} has a suspicious compression ratio; archive rejected.")
        total += info.file_size
        if total > s.zip_max_total_mb * 1024 * 1024:
            raise ArchiveError("Archive expands beyond the configured size limit.")
        with zf.open(info) as f:
            content = f.read(s.zip_max_total_mb * 1024 * 1024 + 1)
        digest = hashlib.sha256(content).hexdigest()
        archived = any(part.lower() in ARCHIVE_DIR_NAMES for part in p.parts[:-1])
        if digest in seen:
            out.duplicates.append((name, seen[digest]))
        else:
            seen[digest] = name
        out.members.append(ArchiveMember(name, content, digest, archived))
    return out


def docx_text(data: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(data))
    parts = []
    for para in doc.paragraphs:
        if para.text.strip():
            style = (para.style.name or "").lower() if para.style is not None else ""
            prefix = "## " if style.startswith("heading") else ("- " if "list" in style else "")
            parts.append(prefix + para.text.strip())
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def pdf_text(data: bytes, max_pages: int = 200) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return [(page.extract_text() or "") for page in reader.pages[:max_pages]]


def pptx_text(data: bytes) -> list[str]:
    """Slide text from a .pptx without extra dependencies (reads the XML parts)."""
    import re

    zf = zipfile.ZipFile(io.BytesIO(data))
    slides = sorted(
        (n for n in zf.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
        key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)),
    )
    out = []
    for n in slides[:300]:
        xml = zf.read(n).decode("utf-8", errors="replace")
        out.append(" ".join(re.findall(r"<a:t>([^<]*)</a:t>", xml)))
    return out


def extract_text(data: bytes, filename: str) -> tuple[str, dict]:
    """Return (text, meta). Meta includes per-page text for PDFs/decks where available."""
    ext = PurePosixPath(filename.lower()).suffix
    if ext == ".docx":
        return docx_text(data), {"format": "docx"}
    if ext == ".pdf":
        pages = pdf_text(data)
        return "\n\n".join(f"[page {i+1}]\n{t}" for i, t in enumerate(pages)), {"format": "pdf", "pages": len(pages)}
    if ext == ".pptx":
        slides = pptx_text(data)
        return "\n\n".join(f"[slide {i+1}]\n{t}" for i, t in enumerate(slides)), {"format": "pptx", "slides": len(slides)}
    if ext in (".txt", ".md", ".csv"):
        return data.decode("utf-8-sig", errors="replace"), {"format": ext.lstrip(".")}
    return "", {"format": ext.lstrip("."), "note": "Text extraction not supported for this file type"}
