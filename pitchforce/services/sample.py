"""Sample mode: create the clearly labeled synthetic event and queue its processing."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Event
from ..sample_data import SAMPLE_EVENT, generate
from .processing import queue_ingest, queue_processing
from .sources import register_file

ORDER = [
    ("company_roster", "company_roster"), ("judge_roster", "judge_roster"), ("transcript", "transcript_txt"),
    ("transcript", "transcript_vtt"), ("scores", "scores"), ("interest", "interest_csv"), ("interest", "interest_xlsx"),
    ("video", "video"),
]


def create_sample_event(db: Session, user_id: str | None, with_video: bool = True) -> Event:
    ev = Event(**SAMPLE_EVENT, is_sample=True, created_by=user_id, status="setup",
               notes="Synthetic sample event. All people, companies, contacts, scores, and responses are fictional.")
    db.add(ev)
    db.flush()
    files = generate(get_settings().data_dir / "sample_files", with_video=with_video)
    for kind, key in ORDER:
        if key not in files:
            continue
        src = register_file(db, ev, kind, Path(files[key]).name, Path(files[key]), user_id)
        queue_ingest(db, ev, src, user_id)
    queue_processing(db, ev, user_id)
    return ev
