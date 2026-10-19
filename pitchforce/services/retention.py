"""Retention and deletion.

Deleting an event removes its stored files and every derived row (database foreign keys cascade
from the event). ``purge_expired`` applies RETENTION_DAYS to archived events.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import utcnow
from ..models import Artifact, Clip, Company, Event, SourceFile
from ..storage import get_storage


def delete_event_files(db: Session, event: Event) -> None:
    st = get_storage()
    for s in db.scalars(select(SourceFile).where(SourceFile.event_id == event.id)):
        st.delete(s.storage_key)
        if (s.meta or {}).get("text_key"):
            st.delete(s.meta["text_key"])
    for a in db.scalars(select(Artifact).where(Artifact.event_id == event.id)):
        st.delete(a.storage_key)
    for c in db.scalars(select(Clip).join(Company, Company.id == Clip.company_id).where(Company.event_id == event.id)):
        if c.storage_key:
            st.delete(c.storage_key)
        for key in (c.validation or {}).get("thumbnails", {}).values():
            st.delete(key)


def purge_event_rows(db: Session, event: Event) -> None:
    eid = event.id
    db.expunge(event)
    db.execute(delete(Event).where(Event.id == eid))
    db.flush()


def purge_expired(db: Session) -> int:
    days = get_settings().retention_days
    if days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    n = 0
    for ev in list(db.scalars(select(Event).where(Event.archived_at.is_not(None), Event.archived_at < cutoff))):
        delete_event_files(db, ev)
        purge_event_rows(db, ev)
        n += 1
    db.commit()
    return n
