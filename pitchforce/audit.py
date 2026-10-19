"""Version / audit history helpers."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .models import AuditLog


def record(
    db: Session,
    action: str,
    entity_type: str,
    entity_id: str = "",
    event_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> AuditLog:
    """Append an audit entry. Callers must not pass contact lists or passcodes here."""
    entry = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id or "",
        event_id=event_id,
        before=before or {},
        after=after or {},
        user_id=user_id,
    )
    db.add(entry)
    return entry
