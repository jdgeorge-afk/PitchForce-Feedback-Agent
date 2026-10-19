"""Identity matching for companies and people.

Principles
- Similar names, the same slot, or a shared email are *evidence*, never proof. Anything other than an
  exact normalized match or an operator-confirmed alias is a proposal that needs review.
- Confirmed aliases and relationships are stored as rows (with who/when/scope) and are re-applied on
  every regeneration, so operator corrections persist.
- Event-scoped corrections apply only to that event. Global scope must be chosen explicitly.
- Related legal entities (parent, licensor, product name...) are recorded as relationships and are
  never merged into one company automatically.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import (
    Company,
    CompanyAlias,
    EntityRelationship,
    Event,
    Person,
    PersonAlias,
    ReviewItem,
    SpeakerMapping,
)
from ..util import norm_company, norm_email, norm_name


@dataclass
class Match:
    target_id: str | None
    status: str  # exact | alias_confirmed | proposed | none | rejected
    score: float
    reason: str

    @property
    def certain(self) -> bool:
        return self.status in ("exact", "alias_confirmed")


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------
def company_aliases(db: Session, company: Company, include_global: bool = True) -> list[CompanyAlias]:
    q = select(CompanyAlias).where(CompanyAlias.company_id == company.id)
    return list(db.scalars(q))


def match_company(db: Session, event: Event, label: str, fuzzy_threshold: float = 86.0) -> Match:
    key = norm_company(label)
    if not key:
        return Match(None, "none", 0, "Empty name")
    companies = list(db.scalars(select(Company).where(Company.event_id == event.id)))
    # 1. Exact match on display name
    for c in companies:
        if norm_company(c.display_name) == key:
            return Match(c.id, "exact", 100, f"Same name as roster company “{c.display_name}”")
    # 2. Confirmed / rejected aliases (event scope, then global scope by name)
    aliases = list(
        db.scalars(
            select(CompanyAlias).join(Company, Company.id == CompanyAlias.company_id).where(Company.event_id == event.id)
        )
    )
    for a in aliases:
        if norm_company(a.alias) == key:
            if a.status == "confirmed":
                c = next(c for c in companies if c.id == a.company_id)
                return Match(c.id, "alias_confirmed", 100, f"Operator-confirmed alias of “{c.display_name}”")
            if a.status == "rejected":
                return Match(a.company_id, "rejected", 0, "Operator rejected this alias")
    for a in db.scalars(select(CompanyAlias).where(CompanyAlias.scope == "global", CompanyAlias.status == "confirmed")):
        if norm_company(a.alias) == key:
            src = db.get(Company, a.company_id)
            if src:
                for c in companies:
                    if norm_company(c.display_name) == norm_company(src.display_name):
                        return Match(c.id, "alias_confirmed", 95, "Global alias confirmed in another event")
    # 3. Fuzzy proposal
    best, best_score = None, 0.0
    for c in companies:
        cands = [c.display_name] + [a.alias for a in aliases if a.company_id == c.id and a.status != "rejected"]
        for cand in cands:
            s = fuzz.ratio(key, norm_company(cand))
            if s > best_score:
                best, best_score = c, s
    if best and best_score >= fuzzy_threshold:
        return Match(best.id, "proposed", best_score, f"Name is similar to “{best.display_name}” ({best_score:.0f}% similar); needs confirmation")
    return Match(None, "none", best_score, "No roster company matches this name")


def add_company_alias(
    db: Session, company: Company, alias: str, source: str, status: str = "proposed", scope: str = "event",
    note: str = "", evidence: dict | None = None, user_id: str | None = None, source_file_id: str | None = None,
) -> CompanyAlias:
    key = norm_company(alias)
    for a in company.aliases:
        if norm_company(a.alias) == key:
            # Never downgrade an operator decision.
            if a.status == "proposed" and status in ("confirmed", "rejected"):
                a.status, a.scope, a.note, a.created_by = status, scope, note or a.note, user_id
            return a
    a = CompanyAlias(
        company_id=company.id, alias=alias, source=source, status=status, scope=scope, note=note,
        evidence=evidence or {}, created_by=user_id, source_file_id=source_file_id,
    )
    db.add(a)
    company.aliases.append(a)
    return a


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------
def match_person(db: Session, event: Event, label: str, email: str = "", roles: tuple[str, ...] | None = None,
                 fuzzy_threshold: float = 88.0) -> Match:
    key = norm_name(label)
    q = select(Person).where(Person.event_id == event.id)
    if roles:
        q = q.where(Person.role.in_(roles))
    people = list(db.scalars(q))
    if not key and not email:
        return Match(None, "none", 0, "No name")
    for p in people:
        if key and norm_name(p.display_name) == key:
            return Match(p.id, "exact", 100, f"Same name as roster entry “{p.display_name}”")
    ids = [p.id for p in people]
    aliases = list(db.scalars(select(PersonAlias).where(PersonAlias.person_id.in_(ids)))) if ids else []
    for a in aliases:
        if key and norm_name(a.alias) == key:
            if a.status == "confirmed":
                p = next(p for p in people if p.id == a.person_id)
                return Match(p.id, "alias_confirmed", 100, f"Operator-confirmed alias of “{p.display_name}”")
            if a.status == "rejected":
                return Match(a.person_id, "rejected", 0, "Operator rejected this alias")
    best, best_score = None, 0.0
    for p in people:
        for cand in [p.display_name] + [a.alias for a in aliases if a.person_id == p.id and a.status != "rejected"]:
            s = max(fuzz.ratio(key, norm_name(cand)), fuzz.token_sort_ratio(key, norm_name(cand)))
            # First-name-only labels such as "Jolene" are common in transcripts.
            first = norm_name(cand).split(" ")[0] if cand else ""
            if key and " " not in key and key == first:
                s = max(s, 90.0)
            if s > best_score:
                best, best_score = p, s
    if best and best_score >= fuzzy_threshold:
        return Match(best.id, "proposed", best_score, f"Similar to “{best.display_name}” ({best_score:.0f}%); needs confirmation")
    e = norm_email(email)
    if e:
        for p in people:
            if norm_email(p.email) == e:
                # Shared email but different name: never auto-merge.
                return Match(p.id, "proposed", 50, f"Email matches roster entry “{p.display_name}” but the name differs; needs review")
    return Match(None, "none", best_score, "No roster person matches")


def add_person_alias(db: Session, person: Person, alias: str, source: str, status: str = "proposed", scope: str = "event",
                     note: str = "", evidence: dict | None = None, user_id: str | None = None) -> PersonAlias:
    key = norm_name(alias)
    for a in person.aliases:
        if norm_name(a.alias) == key:
            if a.status == "proposed" and status in ("confirmed", "rejected"):
                a.status, a.scope, a.note, a.created_by = status, scope, note or a.note, user_id
            return a
    a = PersonAlias(person_id=person.id, alias=alias, source=source, status=status, scope=scope, note=note,
                    evidence=evidence or {}, created_by=user_id)
    db.add(a)
    person.aliases.append(a)
    return a


# ---------------------------------------------------------------------------
# Speakers
# ---------------------------------------------------------------------------
HOST_HINT = re.compile(r"\b(host|moderator|emcee|mc|ecovg|ecosystem venture)\b", re.I)


def map_speakers(db: Session, event: Event, speaker_labels: list[str]) -> dict[str, SpeakerMapping]:
    """Create or refresh speaker->person proposals. Confirmed mappings are kept untouched."""
    existing = {m.speaker_label: m for m in db.scalars(select(SpeakerMapping).where(SpeakerMapping.event_id == event.id))}
    for label in speaker_labels:
        m = existing.get(label)
        if m and m.status in ("confirmed", "rejected"):
            continue
        match = match_person(db, event, label)
        if not m:
            m = SpeakerMapping(event_id=event.id, speaker_label=label)
            db.add(m)
            existing[label] = m
        if match.target_id and match.status != "rejected":
            p = db.get(Person, match.target_id)
            m.person_id = p.id
            m.role = p.role
            m.status = "confirmed_auto" if match.certain else "proposed"
            m.evidence = {"reason": match.reason, "score": match.score}
        else:
            m.person_id = None
            m.role = "host" if HOST_HINT.search(label) else "unknown"
            m.status = "unmatched"
            m.evidence = {"reason": match.reason}
    db.flush()
    return existing


# ---------------------------------------------------------------------------
# Related entities
# ---------------------------------------------------------------------------
RELATION_CUES = [
    (re.compile(r"\b(?:our|the)\s+parent\s+(?:company|corporation)\b[^.]{0,40}?\b([A-Z][\w&\-]+(?:\s+[A-Z][\w&\-]+){0,3})", re.S), "parent"),
    (re.compile(r"\b([A-Z][\w&\-]+(?:\s+[A-Z][\w&\-]+){0,3})\s*(?:,|is|was)?\s*(?:our|the)\s+parent\b"), "parent"),
    (re.compile(r"\b(?:exclusive\s+)?licen[cs]e(?:d)?\s+(?:from|with|by)\s+([A-Z][\w&\-]+(?:\s+[A-Z][\w&\-]+){0,3})"), "licensor"),
    (re.compile(r"\bsubsidiary\s+of\s+([A-Z][\w&\-]+(?:\s+[A-Z][\w&\-]+){0,3})"), "parent"),
    (re.compile(r"\b(?:formerly|previously)\s+(?:known\s+as\s+)?([A-Z][\w&\-]+(?:\s+[A-Z][\w&\-]+){0,2})"), "alternate_name"),
    (re.compile(r"\b(?:doing business as|d/b/a|dba)\s+([A-Z][\w&\-]+(?:\s+[A-Z][\w&\-]+){0,2})"), "trading_name"),
]


def propose_relationship(db: Session, event: Event, company: Company, subject: str, obj: str, relation: str,
                         evidence: dict) -> EntityRelationship:
    for r in db.scalars(select(EntityRelationship).where(EntityRelationship.event_id == event.id, EntityRelationship.company_id == company.id)):
        if norm_company(r.subject_label) == norm_company(subject) and norm_company(r.object_label) == norm_company(obj):
            if r.status == "proposed":
                r.evidence = {**(r.evidence or {}), **evidence}
                if r.relation == "unknown" and relation != "unknown":
                    r.relation = relation  # a more specific cue (e.g. "our parent company") was found
            return r
    r = EntityRelationship(event_id=event.id, company_id=company.id, subject_label=subject, object_label=obj,
                           relation=relation, status="proposed", evidence=evidence)
    db.add(r)
    db.flush()
    return r


def open_review(db: Session, event_id: str | None, kind: str, key: str, title: str, detail: str = "",
                company_id: str | None = None, evidence: dict | None = None, severity: str = "medium",
                target_type: str = "", target_id: str = "", options: list | None = None) -> ReviewItem:
    """Create a review item once (dedupe by key). A resolved item is not reopened unless inputs change the key."""
    item = db.scalar(select(ReviewItem).where(ReviewItem.event_id == event_id, ReviewItem.dedupe_key == key))
    if item:
        if item.status == "open":
            item.title, item.detail, item.evidence = title, detail, evidence or item.evidence
        return item
    item = ReviewItem(event_id=event_id, company_id=company_id, kind=kind, dedupe_key=key, title=title, detail=detail,
                      evidence=evidence or {}, severity=severity, target_type=target_type, target_id=target_id,
                      options=options or [])
    db.add(item)
    db.flush()
    return item


def labels_for_company(db: Session, company: Company) -> list[str]:
    """Names the company may be referred to by: display name + non-rejected aliases."""
    out = [company.display_name]
    for a in company.aliases:
        if a.status != "rejected":
            out.append(a.alias)
    return out


def labels_for_person(person: Person) -> list[str]:
    out = [person.display_name]
    for a in person.aliases:
        if a.status != "rejected":
            out.append(a.alias)
    return out


_ = or_
