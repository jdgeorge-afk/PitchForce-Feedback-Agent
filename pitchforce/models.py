"""Database schema.

Conventions
- Every row has a stable random hex ID. Names and emails are never identity keys.
- Values parsed from uploads keep the original text (``*_raw``) alongside normalized values.
- Derived records carry a ``provenance`` JSON blob: source file id + sha256, row/sheet/cell or
  transcript cue, speaker, interval, original excerpt, interpretation, and model/prompt metadata.
- ``origin`` separates *who* asserted a thing: founder_claim | investor | system_inference | operator.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, new_id, utcnow


def pk() -> Mapped[str]:
    return mapped_column(String(32), primary_key=True, default=new_id)


def fk(target: str, nullable: bool = True, ondelete: str = "CASCADE") -> Mapped:
    return mapped_column(String(32), ForeignKey(target, ondelete=ondelete), nullable=nullable, index=True)


def created() -> Mapped[datetime]:
    return mapped_column(DateTime, default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Users and access
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = pk()
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created()


class EventAccess(Base):
    __tablename__ = "event_access"
    __table_args__ = (UniqueConstraint("event_id", "user_id"),)
    id: Mapped[str] = pk()
    event_id: Mapped[str] = fk("events.id")
    user_id: Mapped[str] = fk("users.id")
    role: Mapped[str] = mapped_column(String(20), default="editor")  # editor | viewer
    created_at: Mapped[datetime] = created()


# ---------------------------------------------------------------------------
# Event and sources
# ---------------------------------------------------------------------------
class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = pk()
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="America/Los_Angeles")
    format: Mapped[str] = mapped_column(String(20), default="virtual")  # virtual | in_person | hybrid
    pitch_minutes: Mapped[float] = mapped_column(Float, default=4.0)
    expected_judge_count: Mapped[int] = mapped_column(Integer, default=6)
    score_min: Mapped[float] = mapped_column(Float, default=1.0)
    score_max: Mapped[float] = mapped_column(Float, default=5.0)
    allow_fractional_scores: Mapped[bool] = mapped_column(Boolean, default=True)
    # none | one_absent_mean (default; only for exactly one confirmed missing judge) | mean_for_confirmed_missing
    missing_judge_policy: Mapped[str] = mapped_column(String(40), default="one_absent_mean")
    score_display_decimals: Mapped[int] = mapped_column(Integer, default=1)
    webinar_id: Mapped[str | None] = mapped_column(String(64))
    replay_link: Mapped[str | None] = mapped_column(String(1000))  # optional Zoom share page (not a source file)
    recording_start_wallclock: Mapped[str | None] = mapped_column(String(32))  # operator-supplied, e.g. 15:00:14
    include_score_in_clip: Mapped[bool] = mapped_column(Boolean, default=False)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(30), default="setup")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)

    companies: Mapped[list["Company"]] = relationship(back_populates="event", order_by="Company.slot_order")
    sources: Mapped[list["SourceFile"]] = relationship(back_populates="event", order_by="SourceFile.created_at")


SOURCE_KINDS = [
    "video", "transcript", "company_roster", "judge_roster", "interest", "scores", "chat",
    "deck", "factsheet", "guidelines", "other",
]


class SourceFile(Base):
    __tablename__ = "source_files"
    id: Mapped[str] = pk()
    event_id: Mapped[str | None] = fk("events.id")  # null for global documents (best practices)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    mime: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    status: Mapped[str] = mapped_column(String(20), default="uploaded")  # uploaded|parsed|failed|superseded|ignored
    # transcripts: wall_clock | media | unknown (operator can override)
    time_basis: Mapped[str | None] = mapped_column(String(20))
    parse_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    parse_error: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    uploaded_by: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    created_at: Mapped[datetime] = created()
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime)

    event: Mapped[Event | None] = relationship(back_populates="sources")


class Upload(Base):
    """A resumable chunked upload in progress."""

    __tablename__ = "uploads"
    id: Mapped[str] = pk()
    event_id: Mapped[str | None] = fk("events.id")
    kind: Mapped[str] = mapped_column(String(30))
    filename: Mapped[str] = mapped_column(String(500))
    total_size: Mapped[int] = mapped_column(Integer)
    received: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="receiving")  # receiving|complete|failed|aborted
    error: Mapped[str | None] = mapped_column(Text)
    source_file_id: Mapped[str | None] = fk("source_files.id", ondelete="SET NULL")
    created_by: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Organizations, companies, people, aliases, relationships
# ---------------------------------------------------------------------------
class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = pk()
    event_id: Mapped[str | None] = fk("events.id")  # null = global record
    name: Mapped[str] = mapped_column(String(300))
    name_raw: Mapped[str] = mapped_column(String(300), default="")
    kind: Mapped[str] = mapped_column(String(30), default="company")  # company | investor_org | other
    created_at: Mapped[datetime] = created()


class Company(Base):
    """A presenting company within one event."""

    __tablename__ = "companies"
    id: Mapped[str] = pk()
    event_id: Mapped[str] = fk("events.id", nullable=False)
    organization_id: Mapped[str | None] = fk("organizations.id", ondelete="SET NULL")
    display_name: Mapped[str] = mapped_column(String(300), nullable=False)
    name_raw: Mapped[str] = mapped_column(String(300), default="")
    slot_order: Mapped[int] = mapped_column(Integer, default=0)
    website: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(30), default="pending")  # pending|processing|ready|failed|approved
    last_error: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = created()

    event: Mapped[Event] = relationship(back_populates="companies")
    aliases: Mapped[list["CompanyAlias"]] = relationship(cascade="all, delete-orphan")


class CompanyAlias(Base):
    __tablename__ = "company_aliases"
    id: Mapped[str] = pk()
    company_id: Mapped[str] = fk("companies.id", nullable=False)
    alias: Mapped[str] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(30))  # roster | transcript | poll | operator | deck
    source_file_id: Mapped[str | None] = fk("source_files.id", ondelete="SET NULL")
    status: Mapped[str] = mapped_column(String(20), default="proposed")  # proposed|confirmed|rejected
    scope: Mapped[str] = mapped_column(String(10), default="event")  # event | global
    note: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


RELATION_TYPES = [
    "same_company", "trading_name", "product_name", "parent", "subsidiary", "licensor", "licensee",
    "alternate_name", "unrelated",
]


class EntityRelationship(Base):
    __tablename__ = "entity_relationships"
    id: Mapped[str] = pk()
    event_id: Mapped[str | None] = fk("events.id")
    company_id: Mapped[str | None] = fk("companies.id")
    subject_label: Mapped[str] = mapped_column(String(300))  # e.g. "Anumio" as heard
    object_label: Mapped[str] = mapped_column(String(300))  # e.g. "Frenelle Pharma" from roster
    relation: Mapped[str] = mapped_column(String(30), default="unknown")
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    scope: Mapped[str] = mapped_column(String(10), default="event")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    confirmed_by: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = created()


class Person(Base):
    __tablename__ = "people"
    id: Mapped[str] = pk()
    event_id: Mapped[str] = fk("events.id", nullable=False)
    display_name: Mapped[str] = mapped_column(String(300))
    name_raw: Mapped[str] = mapped_column(String(300), default="")
    email: Mapped[str | None] = mapped_column(String(300))
    email_raw: Mapped[str | None] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(80))
    affiliation: Mapped[str | None] = mapped_column(String(300))
    title: Mapped[str | None] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20), default="unknown")  # presenter|judge|moderator|host|audience|unknown
    company_id: Mapped[str | None] = fk("companies.id", ondelete="SET NULL")  # presenters
    source_file_id: Mapped[str | None] = fk("source_files.id", ondelete="SET NULL")
    source_ref: Mapped[dict] = mapped_column(JSON, default=dict)
    original: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = created()

    aliases: Mapped[list["PersonAlias"]] = relationship(cascade="all, delete-orphan")


class PersonAlias(Base):
    __tablename__ = "person_aliases"
    id: Mapped[str] = pk()
    person_id: Mapped[str] = fk("people.id", nullable=False)
    alias: Mapped[str] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(30))
    source_file_id: Mapped[str | None] = fk("source_files.id", ondelete="SET NULL")
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    scope: Mapped[str] = mapped_column(String(10), default="event")
    note: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


class JudgeAttendance(Base):
    __tablename__ = "judge_attendance"
    __table_args__ = (UniqueConstraint("event_id", "person_id"),)
    id: Mapped[str] = pk()
    event_id: Mapped[str] = fk("events.id", nullable=False)
    person_id: Mapped[str] = fk("people.id", nullable=False)
    # present | absent_confirmed | abstained_confirmed | unknown
    status: Mapped[str] = mapped_column(String(30), default="unknown")
    source: Mapped[str] = mapped_column(String(30), default="roster")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    confirmed_by: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = created()


class SpeakerMapping(Base):
    """Maps a transcript speaker label to a person (operator-correctable)."""

    __tablename__ = "speaker_mappings"
    __table_args__ = (UniqueConstraint("event_id", "speaker_label"),)
    id: Mapped[str] = pk()
    event_id: Mapped[str] = fk("events.id", nullable=False)
    speaker_label: Mapped[str] = mapped_column(String(300))
    person_id: Mapped[str | None] = fk("people.id", ondelete="SET NULL")
    role: Mapped[str] = mapped_column(String(20), default="unknown")
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = created()


# ---------------------------------------------------------------------------
# Transcript and segmentation
# ---------------------------------------------------------------------------
class Utterance(Base):
    __tablename__ = "utterances"
    __table_args__ = (Index("ix_utt_event_seq", "event_id", "source_file_id", "seq"),)
    id: Mapped[str] = pk()
    event_id: Mapped[str] = fk("events.id", nullable=False)
    source_file_id: Mapped[str] = fk("source_files.id", nullable=False)
    seq: Mapped[int] = mapped_column(Integer)
    cue_ref: Mapped[str] = mapped_column(String(60), default="")  # VTT/SRT cue id or line number
    speaker_label: Mapped[str] = mapped_column(String(300), default="")  # exactly as in source
    speaker_override: Mapped[str | None] = mapped_column(String(300))  # operator correction
    start_raw: Mapped[str] = mapped_column(String(40), default="")
    end_raw: Mapped[str] = mapped_column(String(40), default="")
    start_s: Mapped[float | None] = mapped_column(Float)  # seconds in the source's own time basis
    end_s: Mapped[float | None] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text, default="")
    is_gap: Mapped[bool] = mapped_column(Boolean, default=False)  # [inaudible], missing audio, etc.
    company_id: Mapped[str | None] = fk("companies.id", ondelete="SET NULL")
    assignment_source: Mapped[str] = mapped_column(String(20), default="auto")  # auto | operator

    @property
    def speaker(self) -> str:
        return self.speaker_override or self.speaker_label


SEGMENT_KINDS = ["opening_update", "intro", "pitch", "qa", "feedback", "score", "award"]


class CompanySegment(Base):
    __tablename__ = "company_segments"
    __table_args__ = (UniqueConstraint("company_id", "kind"),)
    id: Mapped[str] = pk()
    company_id: Mapped[str] = fk("companies.id", nullable=False)
    kind: Mapped[str] = mapped_column(String(20))
    transcript_source_id: Mapped[str | None] = fk("source_files.id", ondelete="SET NULL")
    start_utt_id: Mapped[str | None] = fk("utterances.id", ondelete="SET NULL")
    end_utt_id: Mapped[str | None] = fk("utterances.id", ondelete="SET NULL")
    start_s: Mapped[float | None] = mapped_column(Float)  # transcript time basis
    end_s: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(20), default="candidate")  # candidate|needs_review|confirmed
    method: Mapped[str] = mapped_column(String(40), default="heuristic")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[str] = pk()
    company_id: Mapped[str] = fk("companies.id", nullable=False)
    parent_id: Mapped[str | None] = fk("questions.id")
    part_label: Mapped[str] = mapped_column(String(10), default="")  # "a", "b" for multi-part
    seq: Mapped[int] = mapped_column(Integer, default=0)
    asker_person_id: Mapped[str | None] = fk("people.id", ondelete="SET NULL")
    asker_label: Mapped[str] = mapped_column(String(300), default="")
    asker_role: Mapped[str] = mapped_column(String(20), default="investor")  # investor|moderator|audience|unknown
    is_followup: Mapped[bool] = mapped_column(Boolean, default=False)
    utterance_id: Mapped[str | None] = fk("utterances.id", ondelete="SET NULL")
    at_s: Mapped[float | None] = mapped_column(Float)
    at_raw: Mapped[str] = mapped_column(String(40), default="")
    original_excerpt: Mapped[str] = mapped_column(Text, default="")
    paraphrase: Mapped[str] = mapped_column(Text, default="")
    answer_utterance_ids: Mapped[list] = mapped_column(JSON, default=list)
    answer_excerpt: Mapped[str] = mapped_column(Text, default="")
    answer_at_raw: Mapped[str] = mapped_column(String(40), default="")
    answered_summary: Mapped[str] = mapped_column(Text, default="")
    unresolved: Mapped[str] = mapped_column(Text, default="")
    completeness: Mapped[str] = mapped_column(String(20), default="complete")  # complete|incomplete|gap
    importance: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="extracted")  # extracted|confirmed|rejected|edited
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[str] = pk()
    company_id: Mapped[str] = fk("companies.id", nullable=False)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    person_id: Mapped[str | None] = fk("people.id", ondelete="SET NULL")
    speaker_label: Mapped[str] = mapped_column(String(300), default="")
    kind: Mapped[str] = mapped_column(String(20))  # positive|recommendation|concern|nothing_to_add|silent
    point: Mapped[str] = mapped_column(Text, default="")
    theme: Mapped[str] = mapped_column(String(80), default="")
    original_excerpt: Mapped[str] = mapped_column(Text, default="")
    utterance_id: Mapped[str | None] = fk("utterances.id", ondelete="SET NULL")
    at_s: Mapped[float | None] = mapped_column(Float)
    at_raw: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(20), default="extracted")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)


class Claim(Base):
    """Statements to keep separate by origin; presenter claims are never marked verified automatically."""

    __tablename__ = "claims"
    id: Mapped[str] = pk()
    company_id: Mapped[str] = fk("companies.id", nullable=False)
    origin: Mapped[str] = mapped_column(String(20))  # founder_claim|investor|system_inference|operator
    category: Mapped[str] = mapped_column(String(40), default="general")  # regulatory|efficacy|ip|financial|...
    text: Mapped[str] = mapped_column(Text)
    original_excerpt: Mapped[str] = mapped_column(Text, default="")
    utterance_id: Mapped[str | None] = fk("utterances.id", ondelete="SET NULL")
    at_raw: Mapped[str] = mapped_column(String(40), default="")
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_by: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------
class Score(Base):
    __tablename__ = "scores"
    id: Mapped[str] = pk()
    event_id: Mapped[str] = fk("events.id", nullable=False)
    company_id: Mapped[str | None] = fk("companies.id")
    company_label_raw: Mapped[str] = mapped_column(String(300), default="")
    judge_person_id: Mapped[str | None] = fk("people.id", ondelete="SET NULL")
    judge_label_raw: Mapped[str] = mapped_column(String(300), default="")
    value_raw: Mapped[str] = mapped_column(String(60), default="")
    value: Mapped[float | None] = mapped_column(Float)
    # valid | invalid | out_of_range | duplicate | unresolved_identity | unresolved_company | rejected
    status: Mapped[str] = mapped_column(String(30), default="valid")
    status_reason: Mapped[str] = mapped_column(Text, default="")
    source_kind: Mapped[str] = mapped_column(String(20), default="file")  # file | chat | manual
    source_file_id: Mapped[str | None] = fk("source_files.id", ondelete="SET NULL")
    source_ref: Mapped[dict] = mapped_column(JSON, default=dict)
    entered_by: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


class AnnouncedTotal(Base):
    __tablename__ = "announced_totals"
    id: Mapped[str] = pk()
    company_id: Mapped[str] = fk("companies.id", nullable=False)
    value_raw: Mapped[str] = mapped_column(String(60))
    value: Mapped[float | None] = mapped_column(Float)
    denominator: Mapped[float | None] = mapped_column(Float)
    source_kind: Mapped[str] = mapped_column(String(20), default="transcript")  # transcript | manual | file
    source_file_id: Mapped[str | None] = fk("source_files.id", ondelete="SET NULL")
    utterance_id: Mapped[str | None] = fk("utterances.id", ondelete="SET NULL")
    speaker: Mapped[str] = mapped_column(String(300), default="")
    excerpt: Mapped[str] = mapped_column(Text, default="")
    at_raw: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(20), default="candidate")  # candidate|confirmed|rejected
    created_at: Mapped[datetime] = created()


class ScoreResult(Base):
    __tablename__ = "score_results"
    id: Mapped[str] = pk()
    company_id: Mapped[str] = fk("companies.id", nullable=False, ondelete="CASCADE")
    computed_at: Mapped[datetime] = created()
    policy: Mapped[str] = mapped_column(String(40))
    expected_judges: Mapped[int] = mapped_column(Integer)
    valid_count: Mapped[int] = mapped_column(Integer, default=0)
    raw_sum: Mapped[float | None] = mapped_column(Float)
    mean: Mapped[float | None] = mapped_column(Float)
    confirmed_missing: Mapped[int] = mapped_column(Integer, default=0)
    imputed_contribution: Mapped[float | None] = mapped_column(Float)
    adjusted_total: Mapped[float | None] = mapped_column(Float)
    denominator: Mapped[float | None] = mapped_column(Float)
    # verified | verified_with_imputation | incomplete | announced_only | unavailable
    status: Mapped[str] = mapped_column(String(30))
    announced_value: Mapped[float | None] = mapped_column(Float)
    discrepancy: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    input_hash: Mapped[str] = mapped_column(String(64), default="")
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)


# ---------------------------------------------------------------------------
# Investment-interest polls
# ---------------------------------------------------------------------------
INTEREST_CATEGORIES = [
    ("invest", "Interested in investing"),
    ("meeting", "Wants a meeting"),
    ("due_diligence", "Requests due diligence"),
    ("more_info", "Needs more information"),
    ("free_resource", "Offers a free resource"),
    ("introduction", "Offers an introduction"),
    ("mentoring", "Open to mentoring"),
    ("not_interested", "Not interested"),
    ("unknown", "Unknown / unmapped response"),
]
INTEREST_LABELS = dict(INTEREST_CATEGORIES)


class PollLaunch(Base):
    __tablename__ = "poll_launches"
    id: Mapped[str] = pk()
    event_id: Mapped[str] = fk("events.id", nullable=False)
    source_file_id: Mapped[str] = fk("source_files.id", nullable=False)
    launch_index: Mapped[int] = mapped_column(Integer, default=0)
    poll_name: Mapped[str] = mapped_column(String(500), default="")
    webinar_id_raw: Mapped[str] = mapped_column(String(64), default="")
    webinar_id: Mapped[str] = mapped_column(String(64), default="")
    start_raw: Mapped[str] = mapped_column(String(60), default="")
    start_at: Mapped[datetime | None] = mapped_column(DateTime)  # naive, in the file's stated/assumed timezone
    end_raw: Mapped[str] = mapped_column(String(60), default="")
    end_at: Mapped[datetime | None] = mapped_column(DateTime)
    timezone_assumed: Mapped[str] = mapped_column(String(64), default="")
    timezone_status: Mapped[str] = mapped_column(String(20), default="assumed")  # stated|assumed|confirmed
    in_event: Mapped[bool] = mapped_column(Boolean, default=False)
    in_event_reason: Mapped[str] = mapped_column(Text, default="")
    company_id: Mapped[str | None] = fk("companies.id", ondelete="SET NULL")
    company_status: Mapped[str] = mapped_column(String(20), default="unassigned")  # proposed|confirmed|unassigned
    company_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    source_ref: Mapped[dict] = mapped_column(JSON, default=dict)


class PollResponse(Base):
    __tablename__ = "poll_responses"
    id: Mapped[str] = pk()
    event_id: Mapped[str] = fk("events.id", nullable=False)
    source_file_id: Mapped[str] = fk("source_files.id", nullable=False)
    launch_id: Mapped[str | None] = fk("poll_launches.id", ondelete="SET NULL")
    original_response_id: Mapped[str] = mapped_column(String(60), default="")
    name_raw: Mapped[str] = mapped_column(String(300), default="")
    email_raw: Mapped[str] = mapped_column(String(300), default="")
    name: Mapped[str] = mapped_column(String(300), default="")
    email: Mapped[str] = mapped_column(String(300), default="")
    submitted_raw: Mapped[str] = mapped_column(String(60), default="")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    question_raw: Mapped[str] = mapped_column(Text, default="")
    answer_raw: Mapped[str] = mapped_column(Text, default="")
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=False)
    in_event: Mapped[bool] = mapped_column(Boolean, default=False)
    company_id: Mapped[str | None] = fk("companies.id", ondelete="SET NULL")
    superseded_by_id: Mapped[str | None] = fk("poll_responses.id", ondelete="SET NULL")
    duplicate_of_id: Mapped[str | None] = fk("poll_responses.id", ondelete="SET NULL")
    has_conflict: Mapped[bool] = mapped_column(Boolean, default=False)  # contradictory selections
    review_reason: Mapped[str] = mapped_column(Text, default="")
    outreach_status: Mapped[str] = mapped_column(String(30), default="Not started")
    outreach_note: Mapped[str] = mapped_column(Text, default="")
    source_ref: Mapped[dict] = mapped_column(JSON, default=dict)
    original_row: Mapped[dict] = mapped_column(JSON, default=dict)

    choices: Mapped[list["PollResponseChoice"]] = relationship(cascade="all, delete-orphan", order_by="PollResponseChoice.position")


class PollResponseChoice(Base):
    __tablename__ = "poll_response_choices"
    id: Mapped[str] = pk()
    response_id: Mapped[str] = fk("poll_responses.id", nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    choice_raw: Mapped[str] = mapped_column(Text)
    choice_normalized: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(30), default="unknown")
    category_source: Mapped[str] = mapped_column(String(20), default="rule")  # rule | operator


class ContactMatch(Base):
    __tablename__ = "contact_matches"
    id: Mapped[str] = pk()
    event_id: Mapped[str] = fk("events.id", nullable=False)
    response_id: Mapped[str | None] = fk("poll_responses.id")
    person_id: Mapped[str | None] = fk("people.id")
    subject_label: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(20), default="proposed")  # proposed|confirmed|rejected|conflict
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    decided_by: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


class RecommendedAction(Base):
    __tablename__ = "recommended_actions"
    id: Mapped[str] = pk()
    company_id: Mapped[str] = fk("companies.id", nullable=False)
    response_id: Mapped[str | None] = fk("poll_responses.id")
    category: Mapped[str] = mapped_column(String(30))
    action: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(20), default="system_inference")


# ---------------------------------------------------------------------------
# Calendar, best practices, settings
# ---------------------------------------------------------------------------
class CalendarSession(Base):
    __tablename__ = "calendar_sessions"
    __table_args__ = (UniqueConstraint("detail_url"),)
    id: Mapped[str] = pk()
    source_url: Mapped[str] = mapped_column(String(1000))
    detail_url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(String(500))
    start_raw: Mapped[str] = mapped_column(String(120), default="")
    start_at_utc: Mapped[datetime | None] = mapped_column(DateTime)
    timezone: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    verified_on_detail: Mapped[bool] = mapped_column(Boolean, default=False)
    retrieved_at: Mapped[datetime] = created()


class CalendarRecommendation(Base):
    __tablename__ = "calendar_recommendations"
    id: Mapped[str] = pk()
    company_id: Mapped[str] = fk("companies.id", nullable=False)
    session_id: Mapped[str] = fk("calendar_sessions.id", nullable=False)
    reason: Mapped[str] = mapped_column(Text)
    is_upcoming: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="proposed")  # proposed|approved|rejected
    created_at: Mapped[datetime] = created()


class BestPracticeVersion(Base):
    __tablename__ = "best_practice_versions"
    id: Mapped[str] = pk()
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|approved|archived
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    source_docs: Mapped[list] = mapped_column(JSON, default=list)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------
class AlignmentMap(Base):
    """Maps a transcript's time basis onto a video's media time."""

    __tablename__ = "alignment_maps"
    __table_args__ = (UniqueConstraint("event_id", "video_source_id", "transcript_source_id"),)
    id: Mapped[str] = pk()
    event_id: Mapped[str] = fk("events.id", nullable=False)
    video_source_id: Mapped[str] = fk("source_files.id", nullable=False)
    transcript_source_id: Mapped[str] = fk("source_files.id", nullable=False)
    method: Mapped[str] = mapped_column(String(40))  # identity_media|anchor_match|operator_offset|manual
    pieces: Mapped[list] = mapped_column(JSON, default=list)  # [{t_from, t_to, offset}]
    anchors: Mapped[list] = mapped_column(JSON, default=list)
    residual_max_s: Mapped[float | None] = mapped_column(Float)
    drift_detected: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="needs_review")  # ok|needs_review|confirmed|failed
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = created()


CLIP_STATUSES = [
    "boundary_candidate", "alignment_review", "ready_to_render", "rendering", "rendered", "reviewed",
    "shared", "failed", "unavailable",
]


class Clip(Base):
    __tablename__ = "clips"
    __table_args__ = (UniqueConstraint("company_id", "variant"),)
    id: Mapped[str] = pk()
    company_id: Mapped[str] = fk("companies.id", nullable=False)
    variant: Mapped[str] = mapped_column(String(20), default="core")  # core | with_score
    video_source_id: Mapped[str | None] = fk("source_files.id", ondelete="SET NULL")
    start_media_s: Mapped[float | None] = mapped_column(Float)
    end_media_s: Mapped[float | None] = mapped_column(Float)
    boundary_source: Mapped[str] = mapped_column(String(20), default="auto")  # auto | operator
    status: Mapped[str] = mapped_column(String(30), default="boundary_candidate")
    status_reason: Mapped[str] = mapped_column(Text, default="")
    boundary_hash: Mapped[str] = mapped_column(String(64), default="")
    rendered_hash: Mapped[str] = mapped_column(String(64), default="")
    storage_key: Mapped[str | None] = mapped_column(String(500))
    duration_s: Mapped[float | None] = mapped_column(Float)
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    share_url: Mapped[str | None] = mapped_column(String(1000))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Reports, artifacts, sharing
# ---------------------------------------------------------------------------
REPORT_SECTIONS = [
    ("header", "Company, presenter, event and date"),
    ("result", "Result and score"),
    ("assessment", "Overall assessment"),
    ("worked", "What worked"),
    ("questions", "Key investor questions and open points"),
    ("priorities", "Top three improvement priorities"),
    ("best_practices", "Best practices for your follow-up"),
    ("replay", "Replay"),
    ("learning", "Upcoming EcoVG learning opportunities"),
    ("advisory", "Optional advisory support"),
]


class ReportSection(Base):
    __tablename__ = "report_sections"
    __table_args__ = (UniqueConstraint("company_id", "key"),)
    id: Mapped[str] = pk()
    company_id: Mapped[str] = fk("companies.id", nullable=False)
    key: Mapped[str] = mapped_column(String(30))
    position: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(200))
    generated_text: Mapped[str] = mapped_column(Text, default="")
    edited_text: Mapped[str | None] = mapped_column(Text)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    input_hash: Mapped[str] = mapped_column(String(64), default="")  # hash of inputs when last generated
    edited_against_hash: Mapped[str] = mapped_column(String(64), default="")
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    stale_reason: Mapped[str] = mapped_column(Text, default="")
    model_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def text(self) -> str:
        return self.edited_text if self.edited_text is not None else self.generated_text


ARTIFACT_KINDS = [
    "report_pdf", "report_docx", "interest_xlsx", "clip_mp4", "email_txt", "evidence_html", "manifest_json",
    "company_zip", "event_zip", "audit_xlsx", "issues_md", "structured_json",
]


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifact_lookup", "event_id", "company_id", "kind"),)
    id: Mapped[str] = pk()
    event_id: Mapped[str] = fk("events.id", nullable=False)
    company_id: Mapped[str | None] = fk("companies.id")
    kind: Mapped[str] = mapped_column(String(30))
    audience: Mapped[str] = mapped_column(String(20), default="founder")  # founder | organizer
    filename: Mapped[str] = mapped_column(String(300))
    storage_key: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    input_hash: Mapped[str] = mapped_column(String(64), default="")
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    stale_reason: Mapped[str] = mapped_column(Text, default="")
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)
    job_id: Mapped[str | None] = fk("processing_jobs.id", ondelete="SET NULL")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = created()


class ShareLink(Base):
    __tablename__ = "share_links"
    id: Mapped[str] = pk()
    event_id: Mapped[str] = fk("events.id", nullable=False)
    company_id: Mapped[str | None] = fk("companies.id")
    artifact_id: Mapped[str | None] = fk("artifacts.id")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    created_at: Mapped[datetime] = created()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)


# ---------------------------------------------------------------------------
# Review, jobs, audit
# ---------------------------------------------------------------------------
REVIEW_KINDS = [
    "boundary", "alignment", "company_match", "person_match", "related_entity", "speaker", "score",
    "announced_total", "contact", "interest_assignment", "interest_conflict", "timezone", "source_conflict",
    "missing_source", "best_practice_source", "question", "feedback", "clip_validation",
]


class ReviewItem(Base):
    __tablename__ = "review_items"
    __table_args__ = (UniqueConstraint("event_id", "dedupe_key"),)
    id: Mapped[str] = pk()
    event_id: Mapped[str | None] = fk("events.id")
    company_id: Mapped[str | None] = fk("companies.id")
    kind: Mapped[str] = mapped_column(String(30))
    severity: Mapped[str] = mapped_column(String(10), default="medium")  # high | medium | low
    title: Mapped[str] = mapped_column(String(500))
    detail: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    target_type: Mapped[str] = mapped_column(String(40), default="")
    target_id: Mapped[str] = mapped_column(String(32), default="")
    options: Mapped[list] = mapped_column(JSON, default=list)  # allowed resolutions [{value,label}]
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | resolved | dismissed
    dedupe_key: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = created()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


class ReviewDecision(Base):
    __tablename__ = "review_decisions"
    id: Mapped[str] = pk()
    review_item_id: Mapped[str | None] = fk("review_items.id")
    event_id: Mapped[str | None] = fk("events.id")
    decision: Mapped[str] = mapped_column(String(60))
    scope: Mapped[str] = mapped_column(String(10), default="event")
    note: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    user_id: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


JOB_STATUSES = ["queued", "running", "succeeded", "failed", "cancelled"]


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "run_after"),)
    id: Mapped[str] = pk()
    event_id: Mapped[str | None] = fk("events.id")
    company_id: Mapped[str | None] = fk("companies.id")
    parent_id: Mapped[str | None] = fk("processing_jobs.id", ondelete="SET NULL")
    kind: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    stage: Mapped[str] = mapped_column(String(120), default="Waiting to start")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    worker_id: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_estimate_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_by: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    created_at: Mapped[datetime] = created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class AIUsage(Base):
    __tablename__ = "ai_usage"
    id: Mapped[str] = pk()
    event_id: Mapped[str | None] = fk("events.id")
    company_id: Mapped[str | None] = fk("companies.id")
    job_id: Mapped[str | None] = fk("processing_jobs.id", ondelete="SET NULL")
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80))
    purpose: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(40), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    audio_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    est_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = created()


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[str] = pk()
    event_id: Mapped[str | None] = fk("events.id")
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(60))
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    user_id: Mapped[str | None] = fk("users.id", ondelete="SET NULL")
    created_at: Mapped[datetime] = created()
